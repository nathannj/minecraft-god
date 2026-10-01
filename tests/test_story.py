from god_bridge.story import classify_event, coalesce_events, event_targets, watch_matches


def test_god_actions_never_target_a_model_session():
    event = {"type": "god_action", "player_uuid": "alice", "wake_policy": "never"}
    assert not event_targets(event, "alice", "Alice")


def test_pantheon_messages_wake_only_the_recipient():
    event = {
        "type": "god_message",
        "from_player_id": "alice",
        "to_player_id": "bob",
        "wake_policy": "recipient",
    }
    assert not event_targets(event, "alice", "Alice")
    assert event_targets(event, "bob", "Bob")


def test_ordinary_chat_accumulates_but_direct_prayer_wakes():
    assert classify_event({"type": "chat", "message": "nice house"}).action == "accumulate"
    assert classify_event({"type": "chat", "message": "God, answer me"}).action == "wake"
    assert classify_event({"type": "player_prayer", "message": "answer me"}).action == "wake"


def test_dimension_and_advancement_burst_is_coalesced():
    event = coalesce_events([
        {"type": "advancement", "advancement": "minecraft:nether/root"},
        {"type": "dimension_change", "from": "minecraft:overworld", "to": "minecraft:the_nether"},
    ])
    assert event["type"] == "journey_milestone"
    assert event["to"] == "minecraft:the_nether"


def test_story_watch_matches_snapshot_and_block_events():
    assert watch_matches(
        {"kind": "region", "position": "10 64 10", "radius": 3, "dimension": "minecraft:overworld"},
        {"type": "player_snapshot", "position": "12 64 10", "dimension": "minecraft:overworld"},
    )
    assert watch_matches(
        {"kind": "item", "item": "minecraft:diamond", "amount": 3},
        {"type": "player_snapshot", "inventory": {"minecraft:diamond": 4}},
    )
    assert watch_matches(
        {"kind": "block_place", "block": "minecraft:gold_block", "position": "1 2 3"},
        {"type": "blockbereit", "block": "minecraft:gold_block", "position": "1 2 3"},
    ) is False
    assert watch_matches(
        {"kind": "block_place", "block": "minecraft:gold_block", "position": "1 2 3"},
        {"type": "block_place", "block": "minecraft:gold_block", "position": "1 2 3"},
    )


def test_composite_watch_matches_inventory_at_remembered_location():
    watch = {
        "kind": "all",
        "conditions": [
            {"kind": "region", "position": "10 64 10", "radius": 4, "dimension": "minecraft:overworld"},
            {"kind": "item", "item": "minecraft:blaze_rod", "amount": 1},
        ],
    }
    assert watch_matches(watch, {
        "type": "player_snapshot",
        "position": "12 64 10",
        "dimension": "minecraft:overworld",
        "inventory": {"minecraft:blaze_rod": 3},
    })
    assert not watch_matches(watch, {
        "type": "player_snapshot",
        "position": "30 64 10",
        "dimension": "minecraft:overworld",
        "inventory": {"minecraft:blaze_rod": 3},
    })


def test_dangerously_low_health_wakes_without_prayer():
    assert classify_event({"type": "health_threshold", "health": 5.5}).action == "wake"
    assert classify_event({"type": "health_threshold", "health": 12}).action == "accumulate"
