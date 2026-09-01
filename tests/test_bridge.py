import json
import base64
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from god_bridge.bridge import BridgeConfig, GodBridge
from god_bridge.mcp_server import call_tool, config_from_env, handle_request, start_event_server


class FakeRcon:
    def __init__(self):
        self.commands = []

    def command(self, command):
        self.commands.append(command)
        return type("Response", (), {"body": "ok"})()


def make_bridge(tmp_path: Path, dry_run=False):
    rcon = FakeRcon()
    bridge = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json"), dry_run=dry_run), rcon=rcon)
    return bridge, rcon


def test_god_actions_build_expected_commands(tmp_path):
    bridge, rcon = make_bridge(tmp_path)
    result = call_tool(bridge, "spawn_entity", {"entity_type": "minecraft:warden", "position": "~ ~ ~"})
    assert result["ok"] is True
    assert rcon.commands == ["execute as IAmBoreder at @s run summon minecraft:warden ~ ~ ~"]

    call_tool(bridge, "god_say", {"message": "You misunderstand."})
    assert rcon.commands[-1].startswith("tellraw @a ")


def test_god_state_persists(tmp_path):
    bridge, _ = make_bridge(tmp_path, dry_run=True)
    call_tool(bridge, "update_god_state", {"updates": {"disposition": -12, "current_interest": "diamonds"}})
    reloaded = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json"), dry_run=True), rcon=FakeRcon())
    assert reloaded.get_god_state()["disposition"] == -12
    assert reloaded.get_god_state()["current_interest"] == "diamonds"


def test_active_player_overrides_fallback_for_default_actions(tmp_path):
    active_file = tmp_path / "active_player.txt"
    bridge = GodBridge(
        BridgeConfig(
            state_file=str(tmp_path / "state.json"),
            active_player_file=str(active_file),
            player_name="IAmBoreder",
        ),
        rcon=FakeRcon(),
    )
    active_file.write_text("Alex", encoding="utf-8")

    assert bridge.current_player_name() == "Alex"
    result = call_tool(bridge, "give_item", {"item": "minecraft:bread"})
    assert result["command"] == "give Alex minecraft:bread 1"


def test_event_and_chat_buffers(tmp_path):
    bridge, _ = make_bridge(tmp_path, dry_run=True)
    bridge.record_event({"type": "chat", "player": "IAmBoreder", "message": "god please give me food"})
    bridge.record_event({"type": "creeper_explosion", "position": "1 2 3"})
    assert bridge.read_chat(10)[0]["message"] == "god please give me food"
    assert len(bridge.recent_events(10)) == 2


def test_mcp_initialize_and_tool_call(tmp_path):
    bridge, _ = make_bridge(tmp_path, dry_run=True)
    initialized = handle_request(bridge, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert initialized["result"]["serverInfo"]["name"] == "minecraft-god-bridge"
    tools = handle_request(bridge, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert {tool["name"] for tool in tools["result"]["tools"]} >= {"capture_scene", "list_structures", "search_structures", "import_structure", "place_structure"}
    response = handle_request(bridge, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "set_weather", "arguments": {"weather": "thunder"}}})
    assert response["result"]["structuredContent"]["command"] == "weather thunder 600"


def test_command_validation_rejects_newlines(tmp_path):
    bridge, _ = make_bridge(tmp_path, dry_run=True)
    response = handle_request(bridge, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "minecraft_command", "arguments": {"command": "say nope\nstop"}}})
    assert response["result"]["isError"] is True


def test_minecraft_failure_text_is_not_reported_as_success(tmp_path):
    class FailingRcon(FakeRcon):
        def command(self, command):
            self.commands.append(command)
            return type("Response", (), {"body": "No entity was found"})()

    bridge = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json")), rcon=FailingRcon())
    result = call_tool(bridge, "minecraft_command", {"command": "tp @e[type=minecraft:chicken] 1 2 3"})
    assert result["ok"] is False
    assert result["error"]


def test_relative_block_action_is_anchored_to_active_player(tmp_path):
    bridge, rcon = make_bridge(tmp_path)
    call_tool(bridge, "set_block", {"block": "minecraft:blue_wool", "position": "~ ~-1 ~"})
    assert rcon.commands == ["execute as IAmBoreder at @s run setblock ~ ~-1 ~ minecraft:blue_wool"]


def test_place_sign_builds_text_component_strings_and_verifies(tmp_path):
    class SignRcon(FakeRcon):
        def command(self, command):
            self.commands.append(command)
            body = '["{\\"text\\":\\"LOOK CLOSER\\"}"]' if command.startswith("execute as") and "data get block" in command else "Changed the block"
            return type("Response", (), {"body": body})()

    rcon = SignRcon()
    bridge = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json")), rcon=rcon)
    result = call_tool(bridge, "place_sign", {"lines": ["LOOK CLOSER", "- G"]})
    assert result["ok"] is True
    assert "front_text:{messages:['LOOK CLOSER'" in rcon.commands[0]
    assert "data get block" in rcon.commands[1]


def test_sustained_abuse_triggers_an_immediate_consequence(tmp_path):
    bridge, rcon = make_bridge(tmp_path)
    bridge.record_event({"type": "chat", "player": "IAmBoreder", "message": "fuckwit"})
    bridge.record_event({"type": "chat", "player": "IAmBoreder", "message": "pussy"})
    event = bridge.record_event({"type": "chat", "player": "IAmBoreder", "message": "retard"})

    assert event["conduct"]["automatic_consequence"] is True
    assert any("summon minecraft:lightning_bolt" in command for command in rcon.commands)
    assert bridge.get_god_state()["consequence_count"] == 1


def test_capture_scene_returns_private_visual_views(tmp_path):
    class ScanRcon(FakeRcon):
        def __init__(self, render_dir):
            super().__init__()
            self.render_dir = render_dir
            self.render_count = 0

        def command(self, command):
            self.commands.append(command)
            if command.startswith("godscan "):
                return type("Response", (), {"body": json.dumps({
                    "player": "IAmBoreder",
                    "dimension": "minecraft:overworld",
                    "block_position": "0 64 0",
                    "blocks": [
                        {"position": "0 64 0", "type": "minecraft:blue_wool"},
                        {"position": "1 64 0", "type": "minecraft:gold_block"},
                    ],
                    "entities": [],
                })})()
            if command == "camera render":
                self.render_count += 1
                png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
                (self.render_dir / f"render-{self.render_count}.png").write_bytes(png)
            return type("Response", (), {"body": "ok"})()

    render_dir = tmp_path / "plugin-renders"
    render_dir.mkdir()
    rcon = ScanRcon(render_dir)
    bridge = GodBridge(
        BridgeConfig(
            state_file=str(tmp_path / "state.json"),
            camera_output_dir=str(tmp_path / "captures"),
            camera_plugin_render_dir=str(render_dir),
            camera_timeout=5,
        ),
        rcon=rcon,
    )
    result = call_tool(bridge, "capture_scene", {})
    assert result["ok"] is True
    assert {image["view"] for image in result["images"]} == {"front-right", "back-left"}
    response = handle_request(bridge, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "capture_scene", "arguments": {}}})
    assert sum(1 for item in response["result"]["content"] if item["type"] == "image") == 2


def test_json_config_is_loaded(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"player_name": "Alex", "rcon_port": 25580, "dry_run": True}), encoding="utf-8")
    config = config_from_env(str(config_file))
    assert config.player_name == "Alex"
    assert config.rcon_port == 25580
    assert config.dry_run is True


def test_event_http_round_trip(tmp_path):
    bridge, _ = make_bridge(tmp_path, dry_run=True)
    bridge.config.event_port = 0
    server = start_event_server(bridge)
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        request = Request(f"{base}/events", data=json.dumps({"type": "player_join", "player": "IAmBoreder"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            assert response.status == 202
        with urlopen(f"{base}/events?after=0") as response:
            payload = json.loads(response.read())
        assert payload["events"][0]["type"] == "player_join"
    finally:
        server.shutdown()


def scoped_bridge(tmp_path: Path, player_id: str, player_name: str):
    rcon = FakeRcon()
    bridge = GodBridge(
        BridgeConfig(
            state_file=str(tmp_path / "legacy-state.json"),
            profile_dir=str(tmp_path / "players"),
            events_file=str(tmp_path / "events.jsonl"),
            ledger_file=str(tmp_path / "ledger.sqlite3"),
            god_player_id=player_id,
            god_player_name=player_name,
            dry_run=False,
        ),
        rcon=rcon,
    )
    bridge.register_player(player_id, player_name)
    return bridge, rcon


def test_player_god_state_isolated_by_uuid(tmp_path):
    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    alice, _ = scoped_bridge(tmp_path, alice_id, "Alice")
    bob, _ = scoped_bridge(tmp_path, bob_id, "Bob")

    alice.update_god_state({"current_interest": "the old forest"})
    assert alice.get_god_state()["current_interest"] == "the old forest"
    assert bob.get_god_state()["current_interest"] == ""


def test_player_god_cannot_target_another_player(tmp_path):
    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    alice, _ = scoped_bridge(tmp_path, alice_id, "Alice")
    alice.register_player(bob_id, "Bob")

    with pytest.raises(PermissionError):
        call_tool(alice, "effect", {"effect": "glowing", "player": "Bob"})
    with pytest.raises(PermissionError):
        call_tool(alice, "minecraft_command", {"command": "kill Bob"})
    with pytest.raises(PermissionError):
        call_tool(alice, "minecraft_command", {"command": "say @a you are all seen"})

    whisper = call_tool(alice, "whisper_player", {"message": "I see you."})
    assert whisper["private"] is True
    assert whisper["command"].startswith("tellraw Alice ")


def test_intervention_requires_target_god_approval_and_is_one_use(tmp_path):
    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    alice, alice_rcon = scoped_bridge(tmp_path, alice_id, "Alice")
    bob, _ = scoped_bridge(tmp_path, bob_id, "Bob")

    request = alice.request_intervention("Bob", "Alice killed Bob in cold blood", event_id=44)
    request_id = request["request"]["id"]
    assert request["request"]["status"] == "pending"
    with pytest.raises(PermissionError):
        alice.use_intervention(request_id)

    approved = bob.respond_intervention(request_id, "approve", "One strike. No more.")
    assert approved["request"]["status"] == "approved"
    result = alice.use_intervention(request_id)
    assert result["action"] == "approved_smite"
    assert any("execute at Bob run summon minecraft:lightning_bolt" in command for command in alice_rcon.commands)
    with pytest.raises(PermissionError):
        alice.use_intervention(request_id)


def test_intervention_decline_is_final(tmp_path):
    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    alice, _ = scoped_bridge(tmp_path, alice_id, "Alice")
    bob, _ = scoped_bridge(tmp_path, bob_id, "Bob")
    request = alice.request_intervention("Bob", "A grievance", duration=60)
    request_id = request["request"]["id"]
    declined = bob.respond_intervention(request_id, "decline", "Not under my sky.")
    assert declined["request"]["status"] == "declined"
    with pytest.raises(PermissionError):
        alice.use_intervention(request_id)


def test_shared_event_ids_are_unique_across_bridge_processes(tmp_path):
    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    alice, _ = scoped_bridge(tmp_path, alice_id, "Alice")
    bob, _ = scoped_bridge(tmp_path, bob_id, "Bob")
    first = alice.record_event({"type": "player_prayer", "player_uuid": alice_id, "player": "Alice", "message": "one"})
    second = bob.record_event({"type": "player_prayer", "player_uuid": bob_id, "player": "Bob", "message": "two"})
    assert second["event_id"] > first["event_id"]
