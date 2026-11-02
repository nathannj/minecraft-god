import json
import time
import uuid
from collections import deque

import pytest

from god_bridge.bridge import BridgeConfig, GodBridge, assess_conduct
from god_bridge.controller import GodSession, Transcript, _event_for_turn
from god_bridge.log_report import summarize
from god_bridge.mcp_server import call_tool, handle_request
from god_bridge.replay import OfflineAppServer, replay
from god_bridge.story import story_is_active, watch_matches
from god_bridge.story import event_priority


ALICE, BOB, CAROL = [str(uuid.uuid5(uuid.NAMESPACE_DNS, name)) for name in ["test-alice", "test-bob", "test-carol"]]


class PluginRcon:
    def __init__(self):
        self.commands = []

    def command(self, command):
        self.commands.append(command)
        body = "Changed the world"
        if command.startswith("godobserve "):
            body = json.dumps({"ok": True, "position": "0 64 0", "dimension": "minecraft:overworld", "health": 20, "food": 20, "inventory": {}})
        elif command.startswith("godcheckbatch "):
            body = json.dumps({"ok": True, "results": [{"ok": True, "block": {"type": "minecraft:air"}} for _ in command.split()[2:]]})
        elif command.startswith("godvalidate "):
            body = '{"ok":true,"other_players":[]}'
        elif "Dimension" in command:
            body = '"minecraft:overworld"'
        elif "data get entity" in command and " Pos" in command:
            body = "[0.5d, 64.0d, 0.5d]"
        elif command.startswith("godcheck "):
            body = '{"type":"minecraft:air"}'
        return type("Response", (), {"body": body})()


def bridge(root, identifier=ALICE, name="Alice", dry_run=True):
    instance = GodBridge(BridgeConfig(state_file=str(root / "legacy.json"), profile_dir=str(root / "players"), events_file=str(root / "events.jsonl"), ledger_file=str(root / "ledger.db"), god_player_id=identifier, god_player_name=name, dry_run=dry_run), PluginRcon())
    instance.register_player(identifier, name)
    return instance


def session(root):
    return GodSession(ALICE, "Alice", root, Transcript(root), 120, app_server_factory=OfflineAppServer, start_worker=False)


def test_activity_context_survives_full_event_delivery():
    event = {"type": "presence_pulse", "presence_context": {"summary": {"block_place": 20}, "changes": {"inventory_delta": {"minecraft:obsidian": 12}}, "latest_snapshot": {"type": "player_snapshot", "health": 4}, "notable_events": [{"type": "mob_kill", "entity": "minecraft:blaze"}]}}
    delivered = _event_for_turn(deque([event]))
    assert delivered["presence_context"]["changes"]["inventory_delta"]["minecraft:obsidian"] == 12
    assert delivered["presence_context"]["latest_snapshot"]["health"] == 4


def test_recipe_flood_does_not_discard_a_prayer_or_emergency():
    events = [{"type": "player_prayer", "message": "help"}, {"type": "health_threshold", "health": 2}] + [{"type": "advancement", "advancement": str(i)} for i in range(40)]
    kinds = {event["type"] for event in _event_for_turn(deque(events))["events"]}
    assert {"player_prayer", "health_threshold"} <= kinds


@pytest.mark.parametrize("message", ["God, I will kill the dragon for you.", "God, I reached the End.", "God, I need to kill a blaze.", "God, this fucking cave is awful.", "God, this fucking cave is awful, you should see it."])
def test_survival_and_frustration_are_not_direct_abuse(message):
    assert assess_conduct(message)["points"] == 0


def test_direct_threat_still_has_contextual_evidence():
    assert assess_conduct("God, I will kill you")["points"] == 2


@pytest.mark.parametrize("kind", ["block_place", "block_break", "structure_change"])
def test_block_goal_rejects_wrong_dimension(kind):
    watch = {"kind": kind, "dimension": "minecraft:overworld", "position": "0 64 0"}
    assert not watch_matches(watch, {"type": "block_place" if kind == "structure_change" else kind, "position": "0 64 0", "dimension": "minecraft:the_nether"})


def test_cooldown_does_not_flush_story_even_with_an_unfinished_thread():
    assert not story_is_active({"story_phase": "cooldown", "active_thread": "return to library"})


def test_actual_controller_recognizes_library_goal_without_prayer():
    watch = {"id": "library-vow", "created_at": "v1", "objective_id": "vow", "kind": "all", "conditions": [{"kind": "region", "position": "10 64 10", "radius": 2, "dimension": "minecraft:overworld"}, {"kind": "item", "item": "minecraft:blaze_rod"}]}
    state = {"story_watches": [watch], "story_objectives": [{"id": "vow", "status": "active"}]}
    result = replay([{"type": "player_snapshot", "player_uuid": ALICE, "position": "10 64 10", "dimension": "minecraft:overworld", "inventory": {"minecraft:blaze_rod": 1}}], "Alice", ALICE, state)
    assert len(result["milestones"]) == 1
    assert result["state"]["story_objectives"][0]["status"] == "achieved_pending_judgment"


def test_watch_can_be_revised_with_the_same_id(tmp_path):
    god = session(tmp_path)
    for revision in ["v1", "v2"]:
        god.state_path.write_text(json.dumps({"story_watches": [{"id": "same", "created_at": revision, "kind": "item", "item": "minecraft:diamond"}]}))
        assert god.ingest({"type": "item_acquired", "item": "minecraft:diamond", "amount": 1}) == "watch"
    god.close()


def test_emergency_interrupts_construction_and_preserves_unfinished_work(tmp_path):
    god = session(tmp_path)
    god._active_turn, god._active_turn_id = True, "building-turn"
    god._active_started_at = time.monotonic()
    god._active_event = {"type": "player_prayer", "message": "build a library"}
    god._write_turn_context({"turn_id": "building", "status": "active"})
    god.enqueue({"type": "health_threshold", "health": 2})
    god._maybe_interrupt()
    assert ("turn/interrupt", {"threadId": "offline-replay", "turnId": "building-turn"}) in god.codex.requests
    assert json.loads(god.turn_context_path.read_text())["status"] == "interrupted"
    assert any(event["type"] == "deferred_work" for event in god._pending)
    god.close()


def test_late_completion_cannot_clear_a_new_turn(tmp_path):
    god = session(tmp_path)
    god._active_turn, god._active_turn_id = True, "new-turn"
    god.codex.notifications.put({"method": "turn/completed", "params": {"turn": {"id": "old-turn", "status": "interrupted"}}})
    god._drain_notifications()
    assert god._active_turn
    god.close()


def test_interrupted_tools_cannot_mutate_world(tmp_path):
    god = bridge(tmp_path)
    context = tmp_path / "context.json"
    context.write_text('{"status":"interrupted"}')
    god.config.turn_context_file = str(context)
    with pytest.raises(PermissionError, match="interrupted"):
        call_tool(god, "give_item", {"item": "minecraft:obsidian"})


@pytest.mark.parametrize("command", ["kill @e[tag=god_bob_pet,limit=1]", f"kill {BOB}", "execute as @a[tag=god_alice] run kill @s", "function minecraft:danger", "execute store result entity Alice Health float 1 run time query daytime", "data merge entity Alice {Passengers:[{}]}", "execute as Bob at @s run summon minecraft:lightning_bolt ~ ~ ~"])
def test_raw_command_cannot_bypass_ownership(tmp_path, command):
    alice = bridge(tmp_path)
    bridge(tmp_path, BOB, "Bob")
    with pytest.raises(PermissionError):
        call_tool(alice, "minecraft_command", {"command": command})


def test_entity_tag_belongs_to_exact_owner(tmp_path):
    alice, bob = bridge(tmp_path), bridge(tmp_path, BOB, "Bob")
    tag = bob.reserve_entity_tag("god_bob_pet")
    assert bob.execute(f"kill @e[tag={tag},limit=1]")["ok"]
    with pytest.raises(PermissionError):
        alice.execute(f"kill @e[tag={tag},limit=1]")
    with pytest.raises(PermissionError):
        alice.reserve_entity_tag(tag)


def test_protected_home_blocks_indirect_terrain_actions(tmp_path):
    alice, bob = bridge(tmp_path), bridge(tmp_path, BOB, "Bob")
    bob.remember_landmark({"name": "Home", "position": "10 64 10", "bounds": {"min": "8 60 8", "max": "12 70 12"}})
    with pytest.raises(PermissionError, match="protected"):
        call_tool(alice, "fill", {"from": "0 64 0", "to": "20 65 20", "block": "minecraft:lava", "dimension": "minecraft:overworld"})


def test_live_other_player_presence_blocks_collateral_damage(tmp_path):
    alice = bridge(tmp_path, dry_run=False)
    original = alice.rcon.command
    alice.rcon.command = lambda command: type("Response", (), {"body": '{"ok":false,"other_players":["Bob"]}'})() if command.startswith("godvalidate ") else original(command)
    with pytest.raises(PermissionError, match="another player"):
        call_tool(alice, "set_block", {"position": "0 64 0", "dimension": "minecraft:overworld", "block": "minecraft:lava"})
    assert not any(command.startswith("execute in") for command in alice.rcon.commands)


def test_agreement_revision_keeps_old_terms_until_all_accept(tmp_path):
    alice, bob, carol = bridge(tmp_path), bridge(tmp_path, BOB, "Bob"), bridge(tmp_path, CAROL, "Carol")
    first = alice.propose_agreement("Peace", ["Bob"], {"contact": "no contact"})["agreement"]
    with pytest.raises(PermissionError):
        carol.respond_agreement(first["id"], "accept")
    bob.respond_agreement(first["id"], "accept")
    revision = alice.propose_agreement("Peace", ["Bob"], {"contact": "voluntary visits"}, supersedes=first["id"])["agreement"]
    assert bob._ledger.agreement(first["id"])["status"] == "active"
    bob.respond_agreement(revision["id"], "accept")
    assert alice._ledger.agreement(first["id"])["status"] == "superseded"
    assert bob._ledger.agreements_for(BOB, "active")[0]["payload"]["terms"]["contact"] == "voluntary visits"


def test_shared_temple_is_the_same_location_for_both_gods(tmp_path):
    alice, bob = bridge(tmp_path), bridge(tmp_path, BOB, "Bob")
    terms = {"id": "temple", "position": "303 64 -117", "bounds": {"min": "299 60 -125", "max": "315 75 -110"}, "aliases": ["our temple"]}
    agreement = alice.propose_agreement("Shared Temple", ["Bob"], terms, "shared_landmark")["agreement"]
    assert bob.list_landmarks("temple")["count"] == 0
    bob.respond_agreement(agreement["id"], "accept")
    assert alice.list_landmarks("temple")["landmarks"] == bob.list_landmarks("temple")["landmarks"]


def test_accepted_alternative_has_a_real_completion_condition(tmp_path):
    god = bridge(tmp_path)
    objective = god.set_story_objective({"id": "knowledge", "description": "Bring proof of knowledge", "watch": {"kind": "item", "item": "minecraft:blaze_rod"}, "alternatives": [{"description": "Offer a written account instead", "watch": {"kind": "item", "item": "minecraft:written_book"}}]})
    assert watch_matches(objective["watch"], {"type": "item_acquired", "item": "minecraft:written_book", "amount": 1})


def test_batched_inspection_uses_one_server_block_query(tmp_path):
    god = bridge(tmp_path, dry_run=False)
    result = god.inspect_blocks([f"{i} 64 0" for i in range(100)], "minecraft:overworld")
    assert result["ok"] and len(result["results"]) == 100
    assert len(god.rcon.commands) == 1


def test_single_block_budget_prevents_hundreds_of_inspections(tmp_path):
    god = bridge(tmp_path, dry_run=False)
    context = tmp_path / "context.json"
    context.write_text('{"turn_id":"one-decision"}')
    god.config.turn_context_file = str(context)
    for _ in range(12):
        god.inspect_block("0 64 0")
    with pytest.raises(ValueError, match="budget"):
        god.inspect_block("0 65 0")


def test_logs_preserve_error_text_and_count_only_finished_tools(tmp_path):
    transcript = Transcript(tmp_path)
    item = {"id": "call-1", "type": "mcpToolCall", "tool": "register_story_watch", "status": "failed", "result": {"content": [{"type": "text", "text": "watch needs a description"}]}}
    for method in ["item/started", "item/completed"]:
        transcript.write("codex_notification", {"method": method, "params": {"item": item}})
    summary = summarize([tmp_path / "logs" / "god-session.jsonl"])
    assert summary["tool_calls"]["register_story_watch"] == 1
    assert summary["failed_tool_calls"]["register_story_watch"] == 1
    assert summary["recent_errors"][0]["error"] == "watch needs a description"


def test_mcp_errors_have_structured_diagnostics(tmp_path):
    response = handle_request(bridge(tmp_path), {"id": 1, "method": "tools/call", "params": {"name": "minecraft_command", "arguments": {"command": "kill Bob"}}})
    assert response["result"]["structuredContent"]["error_type"] == "PermissionError"


def test_decision_budget_interrupts_and_recovers_the_same_player(tmp_path):
    god = session(tmp_path)
    god._active_turn, god._active_turn_id = True, "slow"
    god._active_started_at = time.monotonic() - god.max_turn_seconds - 1
    god._active_event = {"type": "player_prayer", "message": "build a home"}
    god._write_turn_context({"turn_id": "slow", "status": "active"})
    god._maybe_interrupt()
    assert json.loads(god.turn_context_path.read_text())["status"] == "expired"
    god._interrupt_requested_at = time.monotonic() - 21
    god._recover_stalled_turn()
    assert god._environment["MINECRAFT_GOD_PLAYER_ID"] == ALICE
    assert not god._active_turn
    assert any(event["type"] == "deferred_work" for event in god._pending)
    god.close()


def test_restart_restores_prayers_and_unfinished_work(tmp_path):
    god = session(tmp_path)
    god.enqueue({"type": "player_prayer", "event_id": 17, "message": "help"})
    god._active_turn = True
    god._active_event = {"type": "story_milestone", "summary": "offering completed"}
    with god._condition:
        god._persist_pending()
    god.close()
    restored = session(tmp_path)
    assert [event["type"] for event in restored._pending] == ["player_prayer", "deferred_work"]
    restored.enqueue({"type": "player_prayer", "event_id": 17, "message": "help"})
    assert len(restored._pending) == 2
    restored.close()


def test_prayer_queue_does_not_overflow_or_drop_a_milestone(tmp_path):
    god = session(tmp_path)
    for number in range(80):
        god.enqueue({"type": "player_prayer", "event_id": number + 1})
    god.enqueue({"type": "story_milestone", "watch": {"id": "completed"}})
    assert len(god._pending) == 81
    god.enqueue({"type": "advancement", "event_id": "invalid"})
    assert len(god._pending) == 81
    god.close()


def test_milestone_does_not_hide_its_emergency_priority():
    assert event_priority({"type": "story_milestone", "matched_event": {"type": "health_threshold", "health": 2}}) == 3


def test_durable_events_survive_more_than_the_old_buffer_and_deduplicate(tmp_path):
    god = bridge(tmp_path)
    first = god.record_event({"type": "player_prayer", "event_uuid": "retained-prayer", "player_uuid": ALICE, "message": "help"})
    for index in range(140):
        god.record_event({"type": "advancement", "player_uuid": ALICE, "advancement": str(index)})
    assert god.events_since(0, 200)[0]["message"] == "help"
    restarted = bridge(tmp_path)
    retried = restarted.record_event({"type": "player_prayer", "event_uuid": "retained-prayer", "player_uuid": ALICE, "message": "help"})
    assert first["event_id"] == retried["event_id"]
    assert len(restarted._ledger.events_since(0, 200)) == 141


def test_named_building_refuses_current_player_coordinates(tmp_path):
    god = bridge(tmp_path)
    god.remember_landmark({"name": "Library", "position": "100 64 100", "bounds": {"min": "95 60 95", "max": "105 70 105"}})
    with pytest.raises(ValueError, match="outside"):
        call_tool(god, "set_block", {"block": "minecraft:stone", "position": "0 64 0", "landmark_id": "library"})
    with pytest.raises(ValueError, match="absolute"):
        call_tool(god, "set_block", {"block": "minecraft:stone", "position": "~ ~ ~", "landmark_id": "library"})
    result = call_tool(god, "set_block", {"block": "minecraft:stone", "position": "100 64 100", "landmark_id": "library"})
    assert "execute in minecraft:overworld" in result["command"]


def test_revising_objective_removes_old_watch_and_promise(tmp_path):
    god = bridge(tmp_path)
    god.set_story_objective({"id": "offer", "description": "Bring a rod", "watch": {"id": "rod-watch", "kind": "item", "item": "minecraft:blaze_rod"}})
    god.revise_story_objective("offer", "I prefer to write a book", {"description": "Write a book", "watch": {"id": "book-watch", "kind": "item", "item": "minecraft:written_book"}})
    state = god.get_god_state()
    assert state["unresolved_promises"] == ["Write a book"]
    assert [watch["id"] for watch in state["story_watches"]] == ["book-watch"]
    assert state["story_objectives"][0]["revision_reason"] == "I prefer to write a book"


@pytest.mark.parametrize("command", ["setblock 0 64 0 minecraft:command_block{Command:'kill Bob',auto:1b}", "summon minecraft:command_block_minecart 0 64 0", "data merge block 0 64 0 {Command:'kill Bob'}", "execute at Alice in minecraft:the_nether run setblock ~ ~ ~ minecraft:lava"])
def test_indirect_executable_powers_and_ambiguous_dimension_are_rejected(tmp_path, command):
    with pytest.raises(PermissionError):
        call_tool(bridge(tmp_path), "minecraft_command", {"command": command})


def test_rotated_structure_checks_negative_side_of_origin(tmp_path):
    alice, bob = bridge(tmp_path), bridge(tmp_path, BOB, "Bob")
    catalog = tmp_path / "structures.json"
    catalog.write_text(json.dumps({"entries": [{"id": "god_test:hall", "size": [4, 5, 6], "scoped_safe": True}]}))
    alice.config.structure_catalog_file = str(catalog)
    bob.remember_landmark({"name": "Home", "position": "-4 65 2", "bounds": {"min": "-5 64 0", "max": "-1 70 4"}})
    with pytest.raises(PermissionError, match="protected"):
        alice.place_structure("god_test:hall", "0 64 0", "clockwise_90", dimension="minecraft:overworld")


def test_observed_uuid_requires_an_owned_tag(tmp_path):
    alice, bob = bridge(tmp_path), bridge(tmp_path, BOB, "Bob")
    owned, foreign = alice.reserve_entity_tag(), bob.reserve_entity_tag()
    alice._register_observed_entities({"entities": [{"uuid": "own-entity", "tags": [owned]}, {"uuid": "foreign-entity", "tags": [foreign]}]})
    assert alice._ledger.resource("entity:own-entity")["owner_id"] == ALICE
    assert alice._ledger.resource("entity:foreign-entity") is None


def test_failed_plugin_command_is_not_cached_as_success(tmp_path):
    god = bridge(tmp_path, dry_run=False)
    god.rcon.command = lambda _: type("Response", (), {"body": '{"ok":false,"error":"player_not_found"}'})()
    result = god.execute("godsmite Alice", capability=True, idempotency_key="smite")
    assert not result["ok"]
    assert god._ledger.cached_action(ALICE + ":smite") is None


def test_entity_retry_tag_is_stable_and_owned(tmp_path):
    god = bridge(tmp_path)
    first = god.reserve_entity_tag(idempotency_key="pet-once")
    assert first == god.reserve_entity_tag(idempotency_key="pet-once")
    with pytest.raises(ValueError, match="already reserved"):
        god.reserve_entity_tag(first)


def test_concurrent_profiles_preserve_both_memory_updates(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    left, right = bridge(tmp_path), bridge(tmp_path)
    def update(god, label):
        for index in range(8):
            god.update_god_state({"memories": [f"{label}-{index}"]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update, left, "left"), pool.submit(update, right, "right")]
        for future in futures:
            future.result()
    assert len(left.get_god_state()["memories"]) == 16


def test_many_prayers_survive_batched_delivery():
    pending = deque([{"type": "player_prayer", "event_id": index + 1, "message": str(index)} for index in range(50)])
    delivered = []
    while pending:
        turn = _event_for_turn(pending)
        delivered.extend(turn.get("events", [turn]))
    assert {event["event_id"] for event in delivered} == set(range(1, 51))


def test_failed_interrupt_still_preserves_unfinished_work(tmp_path):
    god = session(tmp_path)
    god._active_turn, god._active_turn_id = True, "slow"
    god._active_started_at = time.monotonic() - god.max_turn_seconds - 1
    god._active_event = {"type": "player_prayer", "message": "build a home"}
    god._write_turn_context({"turn_id": "slow", "status": "active"})
    original = god.codex.request
    def request(method, params=None, **kwargs):
        if method == "turn/interrupt":
            raise TimeoutError("interrupt acknowledgement unavailable")
        return original(method, params, **kwargs)
    god.codex.request = request
    god._maybe_interrupt()
    assert any(event["type"] == "deferred_work" for event in god._pending)
    assert god._interrupt_requested_at
    god.close()


def test_historical_verified_home_is_protected_on_startup(tmp_path):
    profile = tmp_path / "players" / BOB / "god_state.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"landmarks": {"home": {"position": "10 64 10", "dimension": "minecraft:overworld", "visibility": "private", "confidence": "verified"}}}))
    god = bridge(tmp_path)
    with pytest.raises(PermissionError, match="protected"):
        call_tool(god, "set_block", {"block": "minecraft:stone", "position": "10 64 10", "dimension": "minecraft:overworld"})


def test_historical_entity_tags_require_recorded_creation_provenance(tmp_path):
    event = {"type": "god_action", "source": "god", "player_uuid": BOB, "command": "summon minecraft:cow 0 64 0 {Tags:['god_bob_cow']}", "result": "Summoned Cow"}
    (tmp_path / "events.jsonl").write_text(json.dumps(event) + "\n")
    god = bridge(tmp_path)
    assert god._ledger.resource("entity:god_bob_cow")["owner_id"] == BOB
    with pytest.raises(PermissionError):
        god.execute("kill @e[tag=god_bob_cow,limit=1]")


def test_cooldown_respects_offset_time():
    from datetime import datetime, timedelta, timezone
    eligible = (datetime.now(timezone(timedelta(hours=-10))) + timedelta(minutes=10)).isoformat()
    assert not story_is_active({"story_phase": "test", "active_thread": "pending", "next_eligible_intervention_at": eligible})


def test_raw_console_coordinates_do_not_borrow_player_dimension(tmp_path):
    god = bridge(tmp_path)
    bob = bridge(tmp_path, BOB, "Bob")
    bob.remember_landmark({"name": "Overworld Home", "position": "10 64 10"})
    with pytest.raises(PermissionError, match="protected"):
        god.execute("setblock 10 64 10 minecraft:stone")


def test_native_rotation_positions_use_template_origin():
    from god_bridge.bridge import _rotate_structure_position
    assert _rotate_structure_position([1, 2, 3], [4, 5, 6], "clockwise_90") == (-3, 2, 1)
    assert _rotate_structure_position([1, 2, 3], [4, 5, 6], "clockwise_180") == (-1, 2, -3)


def test_dry_run_previews_do_not_count_as_world_changes(tmp_path):
    god = session(tmp_path)
    god._active_started_at = time.monotonic()
    god._record_tool_result({"type": "mcpToolCall", "tool": "place_structure", "result": {"structuredContent": {"ok": True, "preview": True}}})
    assert god._active_stats["world_changes"] == 0
    assert "first_response_seconds" not in god._active_stats
    god.close()


def test_interrupted_import_cannot_activate_or_reload(tmp_path):
    god = bridge(tmp_path)
    context = tmp_path / "turn-context.json"
    context.write_text('{"status":"interrupted"}')
    god.config.turn_context_file = str(context)
    with pytest.raises(PermissionError):
        god.execute("reload", record=False, capability=True)


def test_retry_keys_are_scoped_to_the_god(tmp_path):
    alice, bob = bridge(tmp_path), bridge(tmp_path, BOB, "Bob")
    first = alice.execute("give Alice minecraft:stone 1", idempotency_key="gift")
    second = bob.execute("give Bob minecraft:gold_ingot 1", idempotency_key="gift")
    assert first["command"] != second["command"]
    assert not second.get("idempotent_replay")
    assert alice.execute("give Alice minecraft:stone 1", idempotency_key="gift")["idempotent_replay"]


def test_nested_watch_dimensions_are_normalized(tmp_path):
    god = bridge(tmp_path)
    result = god.register_story_watch({"kind": "all", "description": "Return with proof", "conditions": [{"kind": "region", "position": "0 64 0", "dimension": "overworld"}, {"kind": "item", "item": "minecraft:diamond"}]})
    assert watch_matches(result["watch"], {"type": "player_snapshot", "dimension": "minecraft:overworld", "position": "0 64 0", "inventory": {"minecraft:diamond": 1}})


def test_attention_budget_does_not_evict_an_active_objective(tmp_path):
    god = bridge(tmp_path)
    god.set_story_objective({"id": "offering", "description": "Bring proof", "watch": {"id": "offering", "kind": "item", "item": "minecraft:diamond"}})
    for index in range(25):
        god.register_story_watch({"id": f"hidden-{index}", "kind": "event", "event_type": "sleep", "description": "hidden attention"})
    state = god.get_god_state()
    assert len(state["story_watches"]) == 20
    assert any(watch["id"] == "offering" for watch in state["story_watches"])
