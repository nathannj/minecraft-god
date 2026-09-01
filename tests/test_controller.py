import json

from collections import deque

from god_bridge.controller import CodexAppServer, Transcript, _event_for_turn, _event_targets, turn_text


def test_transcript_writes_readable_and_jsonl_logs(tmp_path):
    transcript = Transcript(tmp_path)
    transcript.write("controller_event", {"type": "player_join", "player": "IAmBoreder"})
    transcript.write(
        "codex_notification",
        {"method": "item/agentMessage/delta", "params": {"delta": "I have arrived."}},
    )

    readable = (tmp_path / "logs" / "god-session.log").read_text(encoding="utf-8")
    records = [json.loads(line) for line in (tmp_path / "logs" / "god-session.jsonl").read_text(encoding="utf-8").splitlines()]

    assert "MINECRAFT EVENT" in readable
    assert "GOD: I have arrived." in readable
    assert records[0]["data"]["type"] == "player_join"
    assert records[1]["data"]["params"]["delta"] == "I have arrived."


def test_minecraft_god_server_requests_are_auto_accepted():
    response = CodexAppServer._server_request_response(
        "mcpServer/elicitation/request",
        {
            "serverName": "minecraft_god",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    )
    assert response == {"action": "accept", "content": {}}
    assert CodexAppServer._server_request_response("unknown/request", {}) is None


def test_app_server_command_overrides_mcp_scope():
    command = CodexAppServer.build_command("codex", {
        "MINECRAFT_GOD_PLAYER_ID": "alice-id",
        "MINECRAFT_GOD_PLAYER": "Alice",
        "MINECRAFT_REQUIRE_GOD_SCOPE": "true",
    })
    assert "mcp_servers.minecraft_god.env.MINECRAFT_GOD_PLAYER_ID='alice-id'" in command
    assert "mcp_servers.minecraft_god.env.MINECRAFT_GOD_PLAYER='Alice'" in command
    assert "mcp_servers.minecraft_god.env.MINECRAFT_REQUIRE_GOD_SCOPE='true'" in command


def test_controller_batches_events_that_arrive_while_god_is_thinking():
    pending = deque([
        {"type": "chat", "message": "first"},
        {"type": "chat", "message": "second"},
    ])
    event = _event_for_turn(pending)
    assert event["type"] == "event_batch"
    assert len(event["events"]) == 2
    assert not pending


def test_turn_text_warns_about_stale_positions_without_periodic_snapshots():
    text = turn_text({"type": "chat", "message": "go"})
    assert "re-observe immediately" in text
    assert "player can move while you reason" in text
    assert "status update merely" in text


def test_death_event_routes_to_victim_and_killer_gods_only():
    death = {
        "type": "player_death",
        "victim_uuid": "victim-id",
        "victim": "Victim",
        "killer_uuid": "killer-id",
        "killer": "Killer",
    }
    assert _event_targets(death, "victim-id", "Victim")
    assert _event_targets(death, "killer-id", "Killer")
    assert not _event_targets(death, "bystander-id", "Bystander")


def test_turn_text_contains_player_ownership_context():
    text = turn_text(None, player_name="Alice", player_id="alice-id")
    assert "personal god of Alice" in text
    assert "directly affect only that player" in text
