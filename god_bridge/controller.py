"""Start Codex App Server and wake the god thread from Minecraft events."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .ledger import GodLedger


ROOT = Path(__file__).resolve().parents[1]
GOD_PROMPT = (ROOT / "prompts" / "god_system.md").read_text(encoding="utf-8")


class Transcript:
    """Persist observable controller and Codex activity for review."""

    def __init__(self, root: Path) -> None:
        log_dir = Path(os.getenv("MINECRAFT_GOD_LOG_DIR", str(root / "logs")))
        log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = log_dir / "god-session.jsonl"
        self.text_path = log_dir / "god-session.log"
        self._lock = threading.Lock()

    def write(self, kind: str, payload: Any) -> None:
        record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "data": payload}
        with self._lock:
            with self.jsonl_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
            with self.text_path.open("a", encoding="utf-8") as stream:
                stream.write(self._summary(record) + "\n")

    @staticmethod
    def _summary(record: dict[str, Any]) -> str:
        kind = record["kind"]
        data = record["data"]
        at = record["at"]
        if kind == "turn_start":
            return f"[{at}] GOD TURN START: {json.dumps(data, ensure_ascii=False)}"
        if kind == "controller_event":
            return f"[{at}] MINECRAFT EVENT: {json.dumps(data, ensure_ascii=False)}"
        if kind == "codex_notification" and isinstance(data, dict):
            method = data.get("method", "notification")
            params = data.get("params", {})
            if method == "item/agentMessage/delta":
                delta = params.get("delta", "") if isinstance(params, dict) else ""
                return f"[{at}] GOD: {delta}"
            if method == "turn/completed":
                return f"[{at}] GOD TURN COMPLETE: {json.dumps(params, ensure_ascii=False)}"
            item = params.get("item", {}) if isinstance(params, dict) else {}
            if isinstance(item, dict) and "tool" in str(item.get("type", "")).lower():
                return f"[{at}] GOD TOOL: {json.dumps(item, ensure_ascii=False)}"
            return f"[{at}] CODEX {method}: {json.dumps(params, ensure_ascii=False)}"
        return f"[{at}] {kind.upper()}: {json.dumps(data, ensure_ascii=False)}"


class CodexAppServer:
    def __init__(self, cwd: Path, transcript: Transcript, environment: dict[str, str] | None = None) -> None:
        self.transcript = transcript
        # Prefer the npm-installed CLI because the desktop bundle can lag behind model availability.
        codex_bin = os.getenv("CODEX_CLI_PATH") or shutil.which("codex.cmd") or shutil.which("codex") or shutil.which("codex.exe") or "codex"
        process_environment = os.environ.copy()
        if environment:
            process_environment.update(environment)
        self.process = subprocess.Popen(
            # The normal desktop notification hook receives the full turn
            # payload. On Windows that can exceed the command-line limit;
            # this autonomous controller does not need desktop notifications.
            [codex_bin, "app-server", "-c", "notify=[]", "--listen", "stdio://"],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_environment,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next_id = 1
        self._pending: dict[int, tuple[threading.Event, list[dict[str, Any] | None]]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        threading.Thread(target=self._read_stdout, name="codex-app-server-reader", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="codex-app-server-stderr", daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                print(f"[codex] invalid JSON: {line.rstrip()}", file=sys.stderr)
                continue
            self.transcript.write("codex_notification" if "method" in message else "codex_response", message)
            if "id" in message:
                request_id = message["id"]
                with self._lock:
                    pending = self._pending.get(request_id)
                if pending:
                    with self._lock:
                        pending[1][0] = message
                        pending[0].set()
                elif "method" in message:
                    self._handle_server_request(message)
            else:
                self.notifications.put(message)

    def _send_message(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            return
        with self._write_lock:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        self.transcript.write("codex_server_request", message)
        response = self._server_request_response(method, params)
        if response is None:
            self.notifications.put(message)
            return
        self.transcript.write("codex_auto_response", {"requestId": message.get("id"), "method": method, "response": response})
        self._send_message({"jsonrpc": "2.0", "id": message.get("id"), "result": response})

    @staticmethod
    def _server_request_response(method: str | None, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == "mcpServer/elicitation/request" and params.get("serverName") == "minecraft_god":
            return {"action": "accept", "content": CodexAppServer._default_form_content(params.get("requestedSchema"))}
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            return {"decision": "accept"}
        return None

    @staticmethod
    def _default_form_content(schema: Any) -> dict[str, Any]:
        if not isinstance(schema, dict):
            return {}
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return {}
        content: dict[str, Any] = {}
        for name, field in properties.items():
            if not isinstance(field, dict):
                continue
            if "default" in field:
                content[name] = field["default"]
            elif isinstance(field.get("enum"), list) and field["enum"]:
                content[name] = field["enum"][0]
            elif field.get("type") == "boolean":
                content[name] = False
            elif field.get("type") == "object":
                content[name] = CodexAppServer._default_form_content(field)
            elif name in schema.get("required", []):
                content[name] = ""
        return content

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            print(f"[codex] {line.rstrip()}", file=sys.stderr)

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        if self.process.stdin is None:
            raise RuntimeError("Codex App Server stdin is unavailable")
        event = threading.Event()
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            result: list[dict[str, Any] | None] = [None]
            self._pending[request_id] = (event, result)
        message = {"method": method, "id": request_id, "params": params or {}}
        self._send_message(message)
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"Timed out waiting for Codex response to {method}")
        with self._lock:
            self._pending.pop(request_id, None)
        response = result[0]
        if response is None:
            raise RuntimeError(f"Codex App Server ended while handling {method}")
        if "error" in response:
            raise RuntimeError(f"Codex {method} failed: {response['error']}")
        return response.get("result", {})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send_message({"method": method, "params": params or {}})

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def fetch_events(base_url: str, after: int) -> dict[str, Any]:
    query = urlencode({"after": after, "limit": 100})
    request = Request(f"{base_url.rstrip('/')}/events?{query}")
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_event_hub(base_url: str, timeout: float = 30.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(fetch_events(base_url, 0).get("latest", 0))
        except Exception:
            time.sleep(0.25)
    raise TimeoutError(f"Event hub did not become ready at {base_url}")


def _event_for_turn(pending_events: deque[dict[str, Any]]) -> dict[str, Any] | None:
    """Drain a burst so a slow model does not answer every stale chat line."""
    if not pending_events:
        return None
    events = list(islice(pending_events, len(pending_events)))
    pending_events.clear()
    if len(events) == 1:
        return events[0]
    return {"type": "event_batch", "events": events}


def turn_text(event: dict[str, Any] | None, first_join: bool = False, idle: bool = False, player_name: str = "", player_id: str = "") -> str:
    if first_join and event and event.get("type") == "god_resume":
        opening = "THE GOD CONTROLLER JUST RESTARTED. Your player may still be online. Re-establish your private context by observing the world, then decide whether to speak or intervene."
    elif first_join:
        opening = "THE PLAYER JUST JOINED THE WORLD. This is your first trigger. Begin by observing the world, then decide whether to speak or intervene."
    elif idle:
        opening = "CONTROL TICK: no new dramatic event was reported. Decide whether observation or silence is the right action."
    else:
        opening = "A new Minecraft event has arrived. Read the current world before deciding what, if anything, to do."
    event_json = json.dumps(event, indent=2) if event else "(none)"
    identity = f"\n\nIDENTITY: You are the personal god of {player_name or 'your assigned player'} ({player_id or 'legacy session'}). You may directly affect only that player. The bridge enforces this boundary. Other gods are independent agents; communicate with them and request permission rather than attempting to target their players."
    return f"{opening}{identity}\n\nEVENT(S):\n{event_json}\n\nYou are always watching, even when silent. Observe the live world before deciding, and re-observe immediately before any location-sensitive action because the player can move while you reason. Never issue a route or coordinate and then perform a delayed action against the old position. Keep travel instructions to one exact, bounded step; after a long action, wait for a confirming event. Do not send a status update merely to announce that you are thinking. Do not narrate a plan instead of acting; either take a confirmed action, or deliberately choose silence."


def _event_targets(event: dict[str, Any], player_id: str, player_name: str) -> bool:
    for key in ("player_uuid", "victim_uuid", "killer_uuid", "target_player_id", "from_player_id", "to_player_id"):
        if str(event.get(key, "")).casefold() == player_id.casefold():
            return True
    return any(player_name and str(event.get(key, "")).casefold() == player_name.casefold() for key in ("player", "victim", "killer", "target"))


class GodSession:
    """One persistent Codex thread and one scoped MCP environment for a player."""

    def __init__(self, player_id: str, player_name: str, root: Path, transcript: Transcript, idle_seconds: float) -> None:
        self.player_id = player_id
        self.player_name = player_name
        self.transcript = transcript
        self.idle_seconds = idle_seconds
        self.codex = CodexAppServer(root, transcript, {
            "MINECRAFT_GOD_PLAYER_ID": player_id,
            "MINECRAFT_GOD_PLAYER": player_name,
            "MINECRAFT_EVENTS_FILE": str(root / "state" / "events.jsonl"),
            "MINECRAFT_LEDGER_FILE": str(root / "state" / "god_ledger.sqlite3"),
            "MINECRAFT_PROFILE_DIR": str(root / "state" / "players"),
            "MINECRAFT_STATE_FILE": str(root / "state" / "players" / player_id / "god_state.json"),
        })
        self.thread_id = ""
        self._pending: deque[dict[str, Any]] = deque(maxlen=64)
        self._condition = threading.Condition()
        self._stopping = False
        self._active_turn = False
        self._first_join_pending = False
        self._last_turn_at = time.monotonic()
        self._start_codex()
        self._worker = threading.Thread(target=self._run, name=f"god-{player_name}", daemon=True)
        self._worker.start()

    def _start_codex(self) -> None:
        self.codex.request("initialize", {"clientInfo": {"name": "minecraft_god_controller", "title": "Minecraft God Controller", "version": "0.2.0"}})
        self.codex.notify("initialized")
        thread = self.codex.request("thread/start", {
            "cwd": str(ROOT),
            "model": os.getenv("MINECRAFT_GOD_MODEL", "gpt-5.6-luna"),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        })
        self.thread_id = thread["thread"]["id"]
        try:
            self.codex.request("thread/name/set", {"threadId": self.thread_id, "name": f"God of {self.player_name}"})
        except Exception as exc:
            print(f"[god:{self.player_name}] could not name Codex thread: {exc}", file=sys.stderr)
        print(f"[god:{self.player_name}] Codex thread ready: {self.thread_id}", file=sys.stderr)

    def enqueue(self, event: dict[str, Any], *, first_join: bool = False) -> None:
        with self._condition:
            self._pending.append(event)
            self._first_join_pending = self._first_join_pending or first_join
            self._condition.notify()

    def _drain_notifications(self) -> None:
        while True:
            try:
                notification = self.codex.notifications.get_nowait()
            except queue.Empty:
                return
            if notification.get("method") == "turn/completed":
                self._active_turn = False

    def _run(self) -> None:
        while not self._stopping and self.codex.process.poll() is None:
            self._drain_notifications()
            if self._active_turn:
                time.sleep(0.2)
                continue
            with self._condition:
                idle = time.monotonic() - self._last_turn_at >= self.idle_seconds
                if not self._pending and not idle:
                    self._condition.wait(timeout=0.5)
                    continue
                event = _event_for_turn(self._pending) if self._pending else None
                first_join = self._first_join_pending
                self._first_join_pending = False
            self._active_turn = True
            self._last_turn_at = time.monotonic()
            self.transcript.write("turn_start", {"player_id": self.player_id, "player": self.player_name, "event": event, "idle": event is None})
            try:
                prompt = GOD_PROMPT + "\n\n" if first_join else ""
                self.codex.request("turn/start", {"threadId": self.thread_id, "input": [{"type": "text", "text": prompt + turn_text(event, first_join=first_join, idle=event is None, player_name=self.player_name, player_id=self.player_id)}]})
            except Exception as exc:
                self._active_turn = False
                print(f"[god:{self.player_name}] turn failed: {exc}", file=sys.stderr)

    def close(self) -> None:
        self._stopping = True
        with self._condition:
            self._condition.notify_all()
        self.codex.close()


def _stable_player_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"minecraft-god:{name.casefold()}"))


def main() -> None:
    event_url = os.getenv("MINECRAFT_EVENT_BASE_URL", "http://127.0.0.1:8765")
    idle_seconds = float(os.getenv("MINECRAFT_GOD_IDLE_SECONDS", "45"))
    ready_file = Path(os.getenv("MINECRAFT_GOD_READY_FILE", str(ROOT / "logs" / "god-controller.ready")))
    transcript = Transcript(ROOT)
    max_gods = max(1, int(os.getenv("MINECRAFT_MAX_GODS", "32")))
    sessions: dict[str, GodSession] = {}
    try:
        cursor = wait_for_event_hub(event_url)
        print(f"[god] Waiting for players to join; ignoring {cursor} old events", file=sys.stderr)
        ready_file.parent.mkdir(parents=True, exist_ok=True)
        ready_file.write_text("ready\n", encoding="utf-8")
        ledger_path = Path(os.getenv("MINECRAFT_LEDGER_FILE", str(ROOT / "state" / "god_ledger.sqlite3")))
        try:
            existing_players = GodLedger(ledger_path).players()
        except Exception as exc:
            existing_players = []
            print(f"[god] could not load existing player profiles: {exc}", file=sys.stderr)
        for profile in existing_players:
            if not profile.get("online") or len(sessions) >= max_gods:
                continue
            player_id = str(profile.get("player_id", "")).strip().lower()
            player_name = str(profile.get("name", "")).strip()
            if not player_id or not player_name:
                continue
            try:
                session = GodSession(player_id, player_name, ROOT, transcript, idle_seconds)
                sessions[player_id] = session
                session.enqueue({"type": "god_resume", "player": player_name, "player_uuid": player_id}, first_join=True)
                transcript.write("controller_resume", {"player_id": player_id, "player": player_name})
                print(f"[god] resumed the personal god of {player_name}", file=sys.stderr)
            except Exception as exc:
                print(f"[god] could not resume {player_name}: {exc}", file=sys.stderr)
        while True:
            try:
                payload = fetch_events(event_url, cursor)
            except Exception as exc:
                print(f"[god] event poll failed: {exc}", file=sys.stderr)
                time.sleep(1)
                continue

            for event in payload.get("events", []):
                cursor = max(cursor, int(event.get("event_id", cursor)))
                event_type = event.get("type")
                if event_type == "player_join":
                    player_name = str(event.get("player", "")).strip()
                    if not player_name:
                        continue
                    player_id = str(event.get("player_uuid", "") or "").strip().lower() or _stable_player_id(player_name)
                    event = dict(event)
                    event.setdefault("player_uuid", player_id)
                    session = sessions.get(player_id)
                    if session is None:
                        if len(sessions) >= max_gods:
                            print(f"[god] max god count {max_gods} reached; ignoring {player_name}", file=sys.stderr)
                            continue
                        session = GodSession(player_id, player_name, ROOT, transcript, idle_seconds)
                        sessions[player_id] = session
                        transcript.write("controller_event", {"routed_to": [player_id], "event": event})
                        print(f"[god] {player_name} joined; creating its personal god", file=sys.stderr)
                    session.player_name = player_name
                    session.enqueue(event, first_join=True)
                    continue

                recipients = [session for session in sessions.values() if _event_targets(event, session.player_id, session.player_name)]
                if recipients:
                    for session in recipients:
                        session.enqueue(event)
                    transcript.write("controller_event", {"routed_to": [session.player_id for session in recipients], "event": event})
            time.sleep(0.5)
    finally:
        ready_file = Path(os.getenv("MINECRAFT_GOD_READY_FILE", str(ROOT / "logs" / "god-controller.ready")))
        ready_file.unlink(missing_ok=True)
        for session in sessions.values():
            session.close()


if __name__ == "__main__":
    main()
