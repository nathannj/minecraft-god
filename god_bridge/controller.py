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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .ledger import GodLedger
from .story import classify_event, coalesce_events, compact_event, event_targets, story_is_active, watch_deadline_due, watch_matches


ROOT = Path(__file__).resolve().parents[1]
GOD_PROMPT = (ROOT / "prompts" / "god_system.md").read_text(encoding="utf-8")


class ControllerAlreadyRunning(RuntimeError):
    """Raised when another controller already owns the event stream."""


class ControllerLock:
    """Process-wide lock preventing two controllers from consuming each event."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._owned = False

    @staticmethod
    def _process_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            # os.kill(pid, 0) is a harmless existence probe on POSIX, but on
            # Windows Python routes every non-CTRL_* value to TerminateProcess.
            # In other words, using signal 0 here kills the process being
            # checked. Query the process handle instead.
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            error_access_denied = 5
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return ctypes.get_last_error() == error_access_denied
            try:
                exit_code = wintypes.DWORD()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    owner_text = self.path.read_text(encoding="ascii").strip()
                    owner_pid = int(owner_text)
                except (FileNotFoundError, ValueError, OSError):
                    owner_pid = None
                if owner_pid is not None and self._process_exists(owner_pid):
                    raise ControllerAlreadyRunning(f"controller already running (pid {owner_pid})")
                if attempt == 0:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise ControllerAlreadyRunning("controller lock is already held")
            else:
                with os.fdopen(fd, "w", encoding="ascii") as stream:
                    stream.write(str(os.getpid()))
                self._owned = True
                return

    def release(self) -> None:
        if not self._owned:
            return
        self._owned = False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class Transcript:
    """Persist observable controller and Codex activity for review."""

    def __init__(self, root: Path) -> None:
        log_dir = Path(os.getenv("MINECRAFT_GOD_LOG_DIR", str(root / "logs")))
        log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = log_dir / "god-session.jsonl"
        self.text_path = log_dir / "god-session.log"
        self._lock = threading.Lock()
        self._max_bytes = max(1_000_000, int(os.getenv("MINECRAFT_GOD_LOG_MAX_BYTES", str(25 * 1024 * 1024))))
        self._rotate_existing()

    def _rotate_existing(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        for path in (self.jsonl_path, self.text_path):
            try:
                oversized = path.stat().st_size >= self._max_bytes
            except FileNotFoundError:
                continue
            if oversized:
                archive = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
                suffix = 1
                while archive.exists():
                    archive = path.with_name(f"{path.stem}-{stamp}-{suffix}{path.suffix}")
                    suffix += 1
                path.replace(archive)

    def write(self, kind: str, payload: Any) -> None:
        record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "data": self._bounded(payload)}
        encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 64 * 1024:
            record["data"] = {"truncated": True, "summary": "Record exceeded 64 KiB after compaction."}
            encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            if any(path.exists() and path.stat().st_size >= self._max_bytes for path in (self.jsonl_path, self.text_path)):
                self._rotate_existing()
            with self.jsonl_path.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
            with self.text_path.open("a", encoding="utf-8") as stream:
                stream.write(self._summary(record) + "\n")

    @classmethod
    def _bounded(cls, value: Any, depth: int = 0) -> Any:
        if depth >= 6:
            return "<depth limit>"
        if isinstance(value, str):
            return value if len(value) <= 4000 else value[:4000] + f"… <{len(value) - 4000} chars omitted>"
        if isinstance(value, dict):
            items = list(value.items())
            compact = {str(key): cls._bounded(item, depth + 1) for key, item in items[:40]}
            if len(items) > 40:
                compact["_omitted_keys"] = len(items) - 40
            return compact
        if isinstance(value, (list, tuple)):
            compact = [cls._bounded(item, depth + 1) for item in value[:20]]
            if len(value) > 20:
                compact.append(f"<{len(value) - 20} items omitted>")
            return compact
        return value

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
    @staticmethod
    def _toml_literal(value: str) -> str:
        """Quote a string for a Codex TOML override without Windows escaping."""
        return "'" + str(value).replace("'", "''") + "'"

    @staticmethod
    def _toml_array(values: list[str]) -> str:
        return "[" + ", ".join(CodexAppServer._toml_literal(value) for value in values) + "]"

    @staticmethod
    def build_command(
        codex_bin: str,
        environment: dict[str, str] | None = None,
        bridge_root: Path | str | None = None,
    ) -> list[str]:
        """Build a dedicated app-server command with an explicitly scoped MCP.

        The Minecraft server is deliberately configured on this controller's
        app-server command instead of relying on the user's global config.
        This prevents unrelated Codex tasks from starting the bridge while
        preserving a complete, per-player environment for each god session.
        """
        root = Path(bridge_root or ROOT).resolve()
        bridge_script = root / "run_bridge.py"
        command = [
            codex_bin,
            "app-server",
            "-c",
            "notify=[]",
            "-c",
            f"mcp_servers.minecraft_god.command={CodexAppServer._toml_literal(sys.executable)}",
            "-c",
            f"mcp_servers.minecraft_god.args={CodexAppServer._toml_array([str(bridge_script), '--no-events'])}",
        ]
        for name, value in (environment or {}).items():
            command.extend([
                "-c",
                f"mcp_servers.minecraft_god.env.{name}={CodexAppServer._toml_literal(str(value))}",
            ])
        command.extend(["--listen", "stdio://"])
        return command

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
            self.build_command(codex_bin, environment, cwd),
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
            if "method" not in message or self._should_log_notification(str(message.get("method", ""))):
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

    @staticmethod
    def _should_log_notification(method: str) -> bool:
        # Token counters, status heartbeats, and token-by-token deltas accounted
        # for most transcript growth while adding no post-session insight.
        return method not in {
            "item/agentMessage/delta",
            "thread/tokenUsage/updated",
            "account/rateLimits/updated",
            "thread/status/changed",
        }

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
    events = list(pending_events)
    pending_events.clear()
    return coalesce_events(events)


def turn_text(event: dict[str, Any] | None, first_join: bool = False, idle: bool = False, player_name: str = "", player_id: str = "") -> str:
    if first_join and event and event.get("type") == "god_resume":
        opening = "THE GOD CONTROLLER JUST RESTARTED. Your player may still be online. Re-establish your private context by observing the world, then decide whether to speak or intervene."
    elif first_join:
        opening = "THE PLAYER JUST JOINED THE WORLD. This is your first trigger. Begin by observing the world, then decide whether to speak or intervene."
    elif event and event.get("type") == "presence_pulse":
        opening = "PRESENCE PULSE: your player has been active without calling you. Observe the live world. Remain silent unless their deeds created a meaningful recognition, danger, consequence, or story transition; never announce that a pulse or monitoring system woke you."
    elif idle:
        opening = "CONTROL TICK: no new dramatic event was reported. Decide whether observation or silence is the right action."
    else:
        opening = "A new Minecraft event has arrived. Read the current world before deciding what, if anything, to do."
    event_json = json.dumps(event, indent=2) if event else "(none)"
    identity = f"\n\nIDENTITY: You are the personal god of {player_name or 'your assigned player'} ({player_id or 'legacy session'}). You may directly affect only that player. The bridge enforces this boundary. Other gods are independent agents; communicate with them and request permission rather than attempting to target their players."
    return f"{opening}{identity}\n\nEVENT(S):\n{event_json}\n\nYou are always watching, even when silent. Observe the live world before deciding, and re-observe immediately before any location-sensitive action because the player can move while you reason. Never issue a route or coordinate and then perform a delayed action against the old position. Keep travel instructions to one exact, bounded step; after a long action, wait for a confirming event. Do not send a status update merely to announce that you are thinking. Do not narrate a plan instead of acting; either take a confirmed action, or deliberately choose silence."


def _event_targets(event: dict[str, Any], player_id: str, player_name: str) -> bool:
    return event_targets(event, player_id, player_name)


class GodSession:
    """One persistent Codex thread and one scoped MCP environment for a player."""

    def __init__(self, player_id: str, player_name: str, root: Path, transcript: Transcript, idle_seconds: float) -> None:
        self.player_id = player_id
        self.player_name = player_name
        self.transcript = transcript
        self.idle_seconds = idle_seconds
        self.story_interval = max(60.0, float(os.getenv("MINECRAFT_GOD_STORY_INTERVAL_SECONDS", "180")))
        self.max_thread_turns = max(3, int(os.getenv("MINECRAFT_GOD_MAX_THREAD_TURNS", "12")))
        self.profile_dir = root / "state" / "players" / player_id
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.profile_dir / "god_state.json"
        self.turn_context_path = self.profile_dir / "turn_context.json"
        self.watch_receipts_path = self.profile_dir / "story_watch_receipts.json"
        self.codex = CodexAppServer(root, transcript, {
            "MINECRAFT_GOD_PLAYER_ID": player_id,
            "MINECRAFT_GOD_PLAYER": player_name,
            "MINECRAFT_EVENTS_FILE": str(root / "state" / "events.jsonl"),
            "MINECRAFT_LEDGER_FILE": str(root / "state" / "god_ledger.sqlite3"),
            "MINECRAFT_PROFILE_DIR": str(root / "state" / "players"),
            "MINECRAFT_RCON_PASSWORD_FILE": str(root / "server" / "rcon-password.txt"),
            "MINECRAFT_STATE_FILE": str(self.state_path),
            "MINECRAFT_ACTIVE_PLAYER_FILE": str(self.profile_dir / "active_player.txt"),
            "MINECRAFT_TURN_CONTEXT_FILE": str(self.turn_context_path),
            "MINECRAFT_REQUIRE_GOD_SCOPE": "true",
        })
        self.thread_id = ""
        self._pending: deque[dict[str, Any]] = deque(maxlen=64)
        self._accumulated: deque[dict[str, Any]] = deque(maxlen=32)
        self._accumulated_score = 0
        self._accumulated_since = 0.0
        self._ambient: deque[dict[str, Any]] = deque(maxlen=64)
        self._ambient_score = 0
        self._ambient_since = 0.0
        self.online = True
        try:
            receipt_values = json.loads(self.watch_receipts_path.read_text(encoding="utf-8"))
            self._triggered_watch_ids: set[str] = {str(value) for value in receipt_values} if isinstance(receipt_values, list) else set()
        except (OSError, json.JSONDecodeError):
            self._triggered_watch_ids = set()
        self._pending_event_ids: set[int] = set()
        self._condition = threading.Condition()
        self._stopping = False
        self._active_turn = False
        self._first_join_pending = False
        self._prompt_required = True
        self._last_turn_at = time.monotonic()
        self._turn_count = 0
        self._codex_initialized = False
        self._start_codex()
        self._worker = threading.Thread(target=self._run, name=f"god-{player_name}", daemon=True)
        self._worker.start()

    def _start_codex(self) -> None:
        old_thread_id = self.thread_id
        if not self._codex_initialized:
            self.codex.request("initialize", {"clientInfo": {"name": "minecraft_god_controller", "title": "Minecraft God Controller", "version": "0.3.0"}})
            self.codex.notify("initialized")
            self._codex_initialized = True
        thread = self.codex.request("thread/start", {
            "cwd": str(self.profile_dir),
            "model": os.getenv("MINECRAFT_GOD_MODEL", "gpt-5.6-luna"),
            "approvalPolicy": "never",
            # The Minecraft MCP actions are the god's controlled capability;
            # Codex currently requires the full sandbox mode for these tools
            # to run with approvalPolicy=never. The prompt forbids shell/file
            # state access, while the bridge enforces player ownership.
            "sandbox": "danger-full-access",
        })
        self.thread_id = thread["thread"]["id"]
        self._turn_count = 0
        self._prompt_required = True
        try:
            self.codex.request("thread/name/set", {"threadId": self.thread_id, "name": f"God of {self.player_name}"})
        except Exception as exc:
            print(f"[god:{self.player_name}] could not name Codex thread: {exc}", file=sys.stderr)
        if old_thread_id:
            try:
                # Each Codex thread owns its MCP subprocesses. Archiving the
                # retired thread releases its bridge instead of accumulating a
                # new run_bridge.py process at every context rotation.
                self.codex.request("thread/archive", {"threadId": old_thread_id})
            except Exception as exc:
                print(f"[god:{self.player_name}] could not archive retired Codex thread {old_thread_id}: {exc}", file=sys.stderr)
        print(f"[god:{self.player_name}] Codex thread ready: {self.thread_id}", file=sys.stderr)

    def _state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _watch_milestones(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        milestones = []
        watches = self._state().get("story_watches", [])
        if not isinstance(watches, list):
            return milestones
        for watch in watches:
            if not isinstance(watch, dict):
                continue
            watch_id = str(watch.get("id", ""))
            if not watch_id or watch_id in self._triggered_watch_ids:
                continue
            if watch_matches(watch, event):
                self._remember_watch_trigger(watch_id)
                self._mark_objective_achieved(watch, event)
                milestones.append({
                    "type": "story_milestone",
                    "source": "story_director",
                    "wake_policy": "always",
                    "player": self.player_name,
                    "player_uuid": self.player_id,
                    "watch": watch,
                    "matched_event": event,
                })
        return milestones

    def _mark_objective_achieved(self, watch: dict[str, Any], event: dict[str, Any]) -> None:
        objective_id = str(watch.get("objective_id", ""))
        if not objective_id:
            return
        state = self._state()
        changed = False
        for objective in state.get("story_objectives", []):
            if isinstance(objective, dict) and objective.get("id") == objective_id and objective.get("status") == "active":
                objective["status"] = "achieved_pending_judgment"
                objective["achieved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                objective["matched_event"] = compact_event(event)
                changed = True
        if changed:
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.state_path)

    def _remember_watch_trigger(self, watch_id: str) -> None:
        self._triggered_watch_ids.add(watch_id)
        temporary = self.watch_receipts_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sorted(self._triggered_watch_ids), separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.watch_receipts_path)

    def _deadline_milestone(self) -> dict[str, Any] | None:
        watches = self._state().get("story_watches", [])
        if not isinstance(watches, list):
            return None
        now = datetime.now(timezone.utc)
        for watch in watches:
            if not isinstance(watch, dict):
                continue
            watch_id = str(watch.get("id", ""))
            if watch_id and watch_id not in self._triggered_watch_ids and watch_deadline_due(watch, now):
                self._remember_watch_trigger(watch_id)
                return {
                    "type": "story_milestone",
                    "source": "story_director",
                    "wake_policy": "always",
                    "player": self.player_name,
                    "player_uuid": self.player_id,
                    "watch": watch,
                    "summary": "A registered story deadline has expired.",
                }
        return None

    def ingest(self, event: dict[str, Any], *, first_join: bool = False) -> str:
        """Classify an event and either wake, accumulate, or merely audit it."""
        event_type = str(event.get("type", "")).casefold()
        if event_type in {"player_join", "god_resume"}:
            self.online = True
        elif event_type == "player_quit":
            self.online = False
        if event_type in {"player_snapshot", "block_place", "block_break", "item_acquired", "mob_kill", "health_threshold"}:
            with self._condition:
                if not self._ambient:
                    self._ambient_since = time.monotonic()
                if event_type == "player_snapshot":
                    self._ambient = deque([item for item in self._ambient if item.get("type") != "player_snapshot"], maxlen=64)
                self._ambient.append(compact_event(event))
                self._ambient_score += {"player_snapshot": 1, "block_place": 1, "block_break": 1, "item_acquired": 1, "mob_kill": 3, "health_threshold": 4}.get(event_type, 1)
        milestones = self._watch_milestones(event)
        if milestones:
            for milestone in milestones:
                self.enqueue(milestone, first_join=first_join)
            return "watch"
        state = self._state()
        decision = classify_event(event, state)
        if decision.action == "audit":
            return "audit"
        if decision.action == "wake":
            with self._condition:
                accumulated = list(self._accumulated)
                self._accumulated.clear()
                self._accumulated_score = 0
                self._accumulated_since = 0.0
                event = self._with_presence_context(event)
            for prior in accumulated:
                self.enqueue(prior)
            self.enqueue(event, first_join=first_join)
            return "wake"
        with self._condition:
            if not self._accumulated:
                self._accumulated_since = time.monotonic()
            self._accumulated.append(event)
            self._accumulated_score += decision.score
            if story_is_active(state) and self._accumulated_score >= 5:
                accumulated = list(self._accumulated)
                self._accumulated.clear()
                self._accumulated_score = 0
                self._accumulated_since = 0.0
            else:
                accumulated = []
        for prior in accumulated:
            self.enqueue(prior)
        return "wake" if accumulated else "accumulate"

    def _with_presence_context(self, event: dict[str, Any]) -> dict[str, Any]:
        if not self._ambient:
            return event
        counts: dict[str, int] = {}
        latest_snapshot: dict[str, Any] | None = None
        notable: list[dict[str, Any]] = []
        for item in self._ambient:
            event_type = str(item.get("type", "unknown"))
            counts[event_type] = counts.get(event_type, 0) + 1
            if event_type == "player_snapshot":
                latest_snapshot = item
            else:
                notable.append(item)
        enriched = dict(event)
        enriched["presence_context"] = {
            "summary": counts,
            "latest_snapshot": latest_snapshot,
            "notable_events": notable[-8:],
        }
        self._ambient.clear()
        self._ambient_score = 0
        self._ambient_since = 0.0
        return enriched

    def _presence_pulse(self) -> dict[str, Any]:
        pulse = self._with_presence_context({
            "type": "presence_pulse",
            "source": "story_director",
            "wake_policy": "always",
            "player": self.player_name,
            "player_uuid": self.player_id,
            "summary": "The player has been active without directly calling upon the god. Observe quietly and speak only if a meaningful story beat has emerged.",
        })
        return pulse

    def enqueue(self, event: dict[str, Any], *, first_join: bool = False) -> None:
        with self._condition:
            event_id = event.get("event_id")
            if event_id not in {None, ""}:
                try:
                    normalized_event_id = int(event_id)
                except (TypeError, ValueError):
                    normalized_event_id = 0
                if normalized_event_id and normalized_event_id in self._pending_event_ids:
                    return
                if normalized_event_id:
                    self._pending_event_ids.add(normalized_event_id)
            if len(self._pending) >= self._pending.maxlen:
                dropped = self._pending.popleft()
                dropped_id = dropped.get("event_id")
                if dropped_id not in {None, ""}:
                    try:
                        self._pending_event_ids.discard(int(dropped_id))
                    except (TypeError, ValueError):
                        pass
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
            deadline = self._deadline_milestone()
            if deadline:
                self.enqueue(deadline)
            with self._condition:
                state = self._state()
                story_flush = bool(
                    self._accumulated
                    and story_is_active(state)
                    and self._accumulated_since
                    and time.monotonic() - self._accumulated_since >= self.story_interval
                )
                if story_flush:
                    for accumulated in self._accumulated:
                        self._pending.append(accumulated)
                    self._accumulated.clear()
                    self._accumulated_score = 0
                    self._accumulated_since = 0.0
                presence_due = bool(
                    self.online
                    and self.idle_seconds > 0
                    and self._ambient
                    and self._ambient_score >= 4
                    and time.monotonic() - self._last_turn_at >= self.idle_seconds
                )
                if not self._pending and presence_due:
                    self._pending.append(self._presence_pulse())
                if not self._pending:
                    self._condition.wait(timeout=0.5)
                    continue
                event = _event_for_turn(self._pending)
                if event:
                    drained = event.get("events", [event]) if event.get("type") == "event_batch" else [event]
                    for drained_event in drained:
                        drained_id = drained_event.get("event_id")
                        if drained_id not in {None, ""}:
                            try:
                                self._pending_event_ids.discard(int(drained_id))
                            except (TypeError, ValueError):
                                pass
                first_join = self._first_join_pending
                self._first_join_pending = False
            self._active_turn = True
            self._last_turn_at = time.monotonic()
            if self._turn_count >= self.max_thread_turns:
                old_thread = self.thread_id
                self._start_codex()
                self.transcript.write("thread_rotated", {"player_id": self.player_id, "player": self.player_name, "old_thread_id": old_thread, "new_thread_id": self.thread_id})
            controller_turn_id = uuid.uuid4().hex
            context = {
                "turn_id": controller_turn_id,
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": event,
                "urgent": bool(event and classify_event(event, self._state()).action == "wake"),
            }
            self.turn_context_path.write_text(json.dumps(context, separators=(",", ":")), encoding="utf-8")
            self.transcript.write("turn_start", {"player_id": self.player_id, "player": self.player_name, "event": event, "idle": False})
            try:
                prompt = GOD_PROMPT + "\n\n" if first_join or self._prompt_required else ""
                self.codex.request("turn/start", {"threadId": self.thread_id, "input": [{"type": "text", "text": prompt + turn_text(event, first_join=first_join, idle=event is None, player_name=self.player_name, player_id=self.player_id)}]})
                self._prompt_required = False
                self._turn_count += 1
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
    idle_seconds = max(0.0, float(os.getenv("MINECRAFT_GOD_IDLE_SECONDS", "120")))
    ready_file = Path(os.getenv("MINECRAFT_GOD_READY_FILE", str(ROOT / "logs" / "god-controller.ready")))
    lock_file = Path(os.getenv("MINECRAFT_GOD_CONTROLLER_LOCK_FILE", str(ROOT / "logs" / "god-controller.lock")))
    controller_lock = ControllerLock(lock_file)
    try:
        controller_lock.acquire()
    except ControllerAlreadyRunning as exc:
        print(f"[god] {exc}; refusing to start a duplicate", file=sys.stderr)
        raise SystemExit(2)
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
                session.ingest({"type": "god_resume", "source": "controller", "wake_policy": "always", "player": player_name, "player_uuid": player_id}, first_join=True)
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
                    new_session = session is None
                    if session is None:
                        if len(sessions) >= max_gods:
                            print(f"[god] max god count {max_gods} reached; ignoring {player_name}", file=sys.stderr)
                            continue
                        session = GodSession(player_id, player_name, ROOT, transcript, idle_seconds)
                        sessions[player_id] = session
                        transcript.write("controller_event", {"routed_to": [player_id], "event": event})
                        print(f"[god] {player_name} joined; creating its personal god", file=sys.stderr)
                    session.player_name = player_name
                    disposition = session.ingest(event, first_join=new_session)
                    if disposition != "audit":
                        transcript.write("controller_event", {"routed_to": [player_id], "disposition": disposition, "event": compact_event(event)})
                    continue

                recipients = [session for session in sessions.values() if _event_targets(event, session.player_id, session.player_name)]
                if recipients:
                    routing = {}
                    for session in recipients:
                        routing[session.player_id] = session.ingest(event)
                    if any(disposition != "audit" for disposition in routing.values()):
                        transcript.write("controller_event", {"routed_to": routing, "event": compact_event(event)})
            time.sleep(0.5)
    finally:
        ready_file = Path(os.getenv("MINECRAFT_GOD_READY_FILE", str(ROOT / "logs" / "god-controller.ready")))
        ready_file.unlink(missing_ok=True)
        for session in sessions.values():
            session.close()
        controller_lock.release()


if __name__ == "__main__":
    main()
