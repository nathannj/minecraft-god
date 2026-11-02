import gzip
import json
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from god_bridge.bridge import BridgeConfig, GodBridge
from god_bridge.codex_runtime import RUNTIME_CONFIG, runtime_environment, verify_mcp_inventory
from god_bridge.controller import CodexAppServer, GodSession, Transcript, fetch_events
from god_bridge.event_http import start_event_server
from god_bridge.events import validate_event
from god_bridge.ledger import GodLedger
from god_bridge.mcp_server import tool_definitions
from god_bridge.replay import OfflineAppServer, replay
from god_bridge.structures import StructureLibrary, read_structure_metadata
from test_structures import _structure_nbt

PLAYER_ID = "a0d35179-5ead-4e3a-bef9-cdbb03bf306d"


@pytest.fixture
def event_service(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("test-event-token")
    monkeypatch.setenv("MINECRAFT_EVENT_TOKEN_FILE", str(token_file))
    bridge = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json"), event_token="test-event-token", event_port=0, dry_run=True))
    server = start_event_server(bridge)
    yield f"http://127.0.0.1:{server.server_port}", bridge
    server.shutdown()
    server.server_close()


def test_event_read_write_authentication_and_controller_poll(event_service):
    base, bridge = event_service
    event = {"type": "player_join", "player": "Alice", "player_uuid": PLAYER_ID}
    for method in ("GET", "POST"):
        request = Request(base + "/events", data=json.dumps(event).encode() if method == "POST" else None, method=method)
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 401
    request = Request(base + "/events", data=json.dumps(event).encode(), headers={"Authorization": "Bearer test-event-token"})
    with urlopen(request) as response:
        assert response.status == 202
    assert fetch_events(base, 0)["events"][0]["player_uuid"] == PLAYER_ID
    assert len(bridge._ledger.players()) == 1


@pytest.mark.parametrize("bad_id", ["../../escaped", "C:\\outside", "/outside", "..", "", None, ["uuid"]])
def test_reject_unsafe_identity_before_profile_or_ledger_writes(tmp_path, bad_id):
    event = {"type": "player_join", "player": "Alice", "player_uuid": bad_id}
    with pytest.raises(ValueError):
        validate_event(event)
    with pytest.raises(ValueError):
        GodLedger(tmp_path / "ledger.db").register_player(bad_id, "Alice")
    with pytest.raises(ValueError):
        GodSession(bad_id, "Alice", tmp_path, Transcript(tmp_path), 120, app_server_factory=OfflineAppServer, start_worker=False)
    with pytest.raises(ValueError):
        replay([], "Alice", bad_id)


def test_http_rejects_invalid_payload_without_state_change(event_service):
    base, bridge = event_service
    payloads = [[], {"type": "god_action"}, {"type": "player_join", "player": "Alice", "player_uuid": "../../outside"},
                {"type": "player_snapshot", "player": "Alice", "player_uuid": PLAYER_ID, "health": float("nan")}]
    for payload in payloads:
        request = Request(base + "/events", data=json.dumps(payload).encode(), headers={"Authorization": "Bearer test-event-token"})
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 400
    assert bridge.latest_event_id() == 0
    assert bridge._ledger.players() == []


def test_event_service_refuses_public_bind_and_missing_token(tmp_path):
    bridge = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json"), event_port=0))
    with pytest.raises(ValueError, match="requires"):
        start_event_server(bridge)
    bridge.config.event_token = "test"
    bridge.config.event_host = "0.0.0.0"
    with pytest.raises(ValueError, match="loopback"):
        start_event_server(bridge)


@pytest.mark.parametrize("kind", ["block_place", "block_break", "mob_kill", "explosion_near_player"])
def test_spatial_event_requires_dimension(kind):
    event = {"type": kind, "player": "Alice", "player_uuid": PLAYER_ID, "position": "1 64 -3", "block": "minecraft:stone"}
    with pytest.raises(ValueError, match="dimension"):
        validate_event(event)
    assert validate_event({**event, "dimension": "minecraft:overworld"})["schema_version"] == 1


def test_single_nbt_import_and_truncated_nbt(tmp_path):
    source = tmp_path / "Altar.nbt"
    source.write_bytes(gzip.compress(_structure_nbt()))
    entries, _ = StructureLibrary(tmp_path)._extract_structures(source, tmp_path / "pack", "demo")
    assert entries[0]["size"] == [4, 5, 6]
    assert entries[0]["id"] == "demo:altar"
    source.write_bytes(b"\x0a")
    with pytest.raises(ValueError, match="truncated"):
        read_structure_metadata(source)


def test_reject_nbt_decompression_bomb(tmp_path):
    source = tmp_path / "bomb.nbt"
    source.write_bytes(gzip.compress(b"\x00" * (32 * 1024 * 1024 + 1)))
    with pytest.raises(ValueError, match="32 MiB"):
        read_structure_metadata(source)


def test_event_poll_uses_ledger_when_audit_file_is_unavailable(tmp_path, monkeypatch):
    bridge = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json"), dry_run=True))
    for index in range(120):
        bridge.record_event({"type": "chat", "message": str(index)})
    original = Path.open
    def reject_audit_read(path, mode="r", *args, **kwargs):
        if path == bridge._events_path and "r" in mode:
            raise AssertionError("event polling read the entire audit log")
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", reject_audit_read)
    bridge._refresh_events()
    assert len(bridge._events) == 100
    assert bridge.latest_event_id() == 120
    assert bridge.events_since(118)[-1]["message"] == "119"


def test_two_gods_cannot_overlap_global_camera_captures(tmp_path, monkeypatch):
    bridges = [GodBridge(BridgeConfig(state_file=str(tmp_path / f"state{i}.json"), dry_run=True)) for i in range(2)]
    active = 0
    maximum = 0
    counter_lock = threading.Lock()
    def capture(scene):
        nonlocal active, maximum
        with counter_lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with counter_lock:
            active -= 1
        return {"ok": True}
    for bridge in bridges:
        monkeypatch.setattr(bridge, "_capture_scanned_scene_unlocked", capture)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda bridge: bridge._capture_scanned_scene({"ok": True}), bridges))
    assert all(result["ok"] for result in results)
    assert maximum == 1


def test_isolated_codex_runtime_does_not_copy_user_configuration(tmp_path, monkeypatch):
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "config.toml").write_text("secret-setting = 'unchanged'")
    monkeypatch.setenv("CODEX_HOME", str(personal))
    environment = runtime_environment(tmp_path / "game")
    assert environment["CODEX_HOME"] != str(personal)
    assert (personal / "config.toml").read_text() == "secret-setting = 'unchanged'"
    config = tomllib.loads(RUNTIME_CONFIG)
    assert not config["features"]["plugins"]
    assert config["sandbox_mode"] == "read-only"
    assert not (Path(environment["CODEX_HOME"]) / "auth.json").exists()


def test_inventory_gate_rejects_extra_servers_and_missing_tools():
    class Client:
        def __init__(self, data): self.data = data
        def request(self, *args, **kwargs): return {"data": self.data}
    valid = {"name": "minecraft_god", "tools": {tool["name"]: tool for tool in tool_definitions()}}
    verify_mcp_inventory(Client([valid]), "test-thread")
    for servers in [[], [valid, {"name": "personal_files"}], [{**valid, "tools": {}}]]:
        with pytest.raises(RuntimeError):
            verify_mcp_inventory(Client(servers), "test-thread")


def test_codex_toml_handles_apostrophes_and_windows_paths():
    value = "C:\\Users\\O'Brien\\Game"
    assert tomllib.loads("path=" + CodexAppServer._toml_literal(value))["path"] == value


def test_demo_runs_without_network_or_model_calls(monkeypatch):
    import socket
    from god_bridge.demo import run_demo
    def no_network(*args, **kwargs):
        raise AssertionError("offline demo attempted network access")
    monkeypatch.setattr(socket, "create_connection", no_network)
    result = run_demo()
    assert result["milestones"] == 1
    assert result["model_calls"] == 0
    assert result["other_player_events_ignored"] == 1


def test_turn_budget_survives_restart_and_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("MINECRAFT_GOD_MAX_TURNS_PER_HOUR", "2")
    first = GodSession("alice-id", "Alice", tmp_path, Transcript(tmp_path), 120, app_server_factory=OfflineAppServer, start_worker=False)
    first._record_turn_start()
    first._record_turn_start()
    first.close()
    second = GodSession("alice-id", "Alice", tmp_path, Transcript(tmp_path), 120, app_server_factory=OfflineAppServer, start_worker=False)
    assert not second._turn_budget_available()
    second._turn_starts = [time.time() - 3601]
    assert second._turn_budget_available()
    second.close()


def test_turn_metrics_sum_token_updates_without_double_counting(tmp_path):
    session = GodSession("alice-id", "Alice", tmp_path, Transcript(tmp_path), 120, app_server_factory=OfflineAppServer, start_worker=False)
    session._active_turn = True
    for total in [10, 25, 25]:
        session.codex.notifications.put({"method": "thread/tokenUsage/updated", "params": {"threadId": session.thread_id, "tokenUsage": {"total": {"totalTokens": total}}}})
    session._drain_notifications()
    assert session._active_stats["tokens"]["totalTokens"] == 25
    session.close()
