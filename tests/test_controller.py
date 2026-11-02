import json

from collections import deque

from god_bridge.controller import ControllerAlreadyRunning, ControllerLock, CodexAppServer, Transcript, _event_for_turn, _event_targets, turn_text
from god_bridge.controller import GodSession
from god_bridge.replay import OfflineAppServer


def test_transcript_writes_readable_and_jsonl_logs(tmp_path):
    transcript = Transcript(tmp_path)
    transcript.write("controller_event", {"type": "player_join", "player": "Player"})
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
    }, r"E:\MinecraftAiGod")
    assert any(item.startswith("mcp_servers.minecraft_god.command=") for item in command)
    assert any("mcp_servers.minecraft_god.args=" in item and "god_bridge.mcp_server" in item for item in command)
    assert 'mcp_servers.minecraft_god.env.MINECRAFT_GOD_PLAYER_ID="alice-id"' in command
    assert 'mcp_servers.minecraft_god.env.MINECRAFT_GOD_PLAYER="Alice"' in command
    assert 'mcp_servers.minecraft_god.env.MINECRAFT_REQUIRE_GOD_SCOPE="true"' in command


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


def test_controller_lock_allows_only_one_owner(tmp_path):
    lock_path = tmp_path / "god-controller.lock"
    first = ControllerLock(lock_path)
    second = ControllerLock(lock_path)
    first.acquire()
    try:
        assert lock_path.read_text(encoding="ascii")
        try:
            second.acquire()
        except ControllerAlreadyRunning:
            pass
        else:
            raise AssertionError("a second controller acquired the lock")
    finally:
        first.release()
    second.acquire()
    second.release()


def test_god_model_and_effort_are_explicit_on_threads_and_turns(tmp_path, monkeypatch):
    monkeypatch.delenv("MINECRAFT_GOD_MODEL", raising=False)
    monkeypatch.delenv("MINECRAFT_GOD_REASONING_EFFORT", raising=False)
    god = GodSession("alice-id", "Alice", tmp_path, Transcript(tmp_path), 120,
                     app_server_factory=OfflineAppServer, start_worker=False)
    original = god.codex.request

    def request(method, params=None, **kwargs):
        result = original(method, params, **kwargs)
        if method == "turn/start":
            god._stopping = True
        return result

    god.codex.request = request
    # Exercise rotation as well as initial creation and an actual worker turn.
    god._turn_count = god.max_thread_turns
    god.enqueue({"type": "player_prayer", "player_uuid": "alice-id", "message": "Are you watching?"})
    god._run()
    thread_requests = [params for method, params in god.codex.requests if method == "thread/start"]
    assert len(thread_requests) == 2
    assert all(params["model"] == "gpt-6-luna" for params in thread_requests)
    assert all(params["config"]["model_reasoning_effort"] == "low" for params in thread_requests)
    turn = next(params for method, params in god.codex.requests if method == "turn/start")
    assert turn["effort"] == "low"
    records = [json.loads(line) for line in god.transcript.jsonl_path.read_text(encoding="utf-8").splitlines()]
    started = next(record["data"] for record in records if record["kind"] == "turn_start")
    assert (started["model"], started["reasoning_effort"]) == ("gpt-6-luna", "low")
    god.close()


def test_god_model_overrides_survive_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("MINECRAFT_GOD_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("MINECRAFT_GOD_REASONING_EFFORT", "medium")
    god = GodSession("alice-id", "Alice", tmp_path, Transcript(tmp_path), 120,
                     app_server_factory=OfflineAppServer, start_worker=False)
    god._start_codex()
    thread_requests = [params for method, params in god.codex.requests if method == "thread/start"]
    assert all(params["model"] == "gpt-5.6-luna" for params in thread_requests)
    assert all(params["config"]["model_reasoning_effort"] == "medium" for params in thread_requests)
    god.close()
