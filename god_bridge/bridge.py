"""State, event intake, and high-level god actions."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .rcon import RconClient
from .ledger import GodLedger, expiry_after
from .structures import StructureLibrary


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _epoch_string(value: float) -> str:
    return str(round(float(value), 3))


def _parse_epoch(value: Any) -> float | None:
    try:
        return float(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def assess_conduct(message: str) -> dict[str, Any]:
    """Score sustained abuse without treating ordinary frustration as blasphemy."""
    normalized = re.sub(r"\s+", " ", message.casefold()).strip()
    profanity = re.findall(r"\b(?:fuck\w*|pussy|retard\w*|nonce|dipshit|bitch\w*|cunt\w*)\b", normalized)
    threat = bool(re.search(r"\b(?:kill|end|open your|sword up|burn me alive)\b", normalized))
    points = min(2, len(profanity)) + (2 if threat else 0)
    if points == 0:
        return {"points": 0, "severity": "none", "reason": "ordinary speech"}
    severity = "severe" if points >= 3 else "abusive"
    return {"points": points, "severity": severity, "reason": "direct abuse or threat"}


@dataclass
class BridgeConfig:
    rcon_host: str = "127.0.0.1"
    rcon_port: int = 25575
    rcon_password: str = ""
    player_name: str = "IAmBoreder"
    god_player_id: str = ""
    god_player_name: str = ""
    profile_dir: str = "state/players"
    active_player_file: str = "state/active_player.txt"
    event_host: str = "127.0.0.1"
    event_port: int = 8765
    event_token: str = ""
    state_file: str = "state/god_state.json"
    events_file: str | None = None
    ledger_file: str | None = None
    max_gods: int = 32
    require_god_scope: bool = False
    dry_run: bool = False
    max_command_length: int = 512
    camera_output_dir: str = "state/captures"
    camera_plugin_render_dir: str = "plugins/SpectatorCamera/renders"
    camera_timeout: int = 120
    structure_catalog_file: str = "state/structure_catalog.json"
    structure_datapacks_dir: str = "server/world/datapacks"
    structure_download_dir: str = "state/structure_downloads"
    structure_max_download_mb: int = 64
    minecraft_version: str = "26.2"


DEFAULT_GOD_STATE = {
    "disposition": 0,
    "opinion_of_player": "",
    "judgment_basis": "",
    "preferred_treatment": "",
    "current_interest": "",
    "current_plan": "",
    "chaos_budget": "low",
    "last_major_intervention": "",
    "conduct_score": 0,
    "conduct_window_started_at": "",
    "last_consequence_at": "",
    "consequence_count": 0,
    "memories": [],
}


def _format_number(value: float) -> str:
    """Format a camera coordinate without scientific notation."""
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _camera_points(scan: dict[str, Any]) -> list[tuple[str, tuple[str, float, float, float, float, float]]]:
    """Choose two unobtrusive angles that look toward the observed blocks."""
    raw_player = str(scan.get("position", "")).split()
    if len(raw_player) >= 3:
        try:
            fallback = tuple(float(value) for value in raw_player[:3])  # type: ignore[assignment]
        except ValueError:
            fallback = (0.0, 64.0, 0.0)
    else:
        raw = str(scan.get("block_position", "0 64 0")).split()
        try:
            fallback = tuple(float(value) for value in (raw + ["0", "64", "0"])[:3])  # type: ignore[assignment]
        except ValueError:
            fallback = (0.0, 64.0, 0.0)

    positions: list[tuple[float, float, float]] = []
    for block in scan.get("blocks", []):
        if not isinstance(block, dict):
            continue
        raw_position = str(block.get("position", "")).split()
        if len(raw_position) < 3:
            continue
        try:
            positions.append(tuple(float(value) for value in raw_position[:3]))  # type: ignore[arg-type]
        except ValueError:
            continue
    if not positions:
        positions = [fallback]

    min_x = min(position[0] for position in positions)
    max_x = max(position[0] for position in positions)
    min_y = min(position[1] for position in positions)
    max_y = max(position[1] for position in positions)
    min_z = min(position[2] for position in positions)
    max_z = max(position[2] for position in positions)
    target = ((min_x + max_x + 1.0) / 2.0, (min_y + max_y + 1.0) / 2.0, (min_z + max_z + 1.0) / 2.0)
    extent = max(max_x - min_x + 1.0, max_z - min_z + 1.0, 4.0)
    distance = min(max(extent * 1.35 + 3.0, 7.0), 48.0)
    dimension = str(scan.get("dimension", "minecraft:overworld"))
    world = {
        "minecraft:overworld": "world",
        "minecraft:the_nether": "world_nether",
        "minecraft:the_end": "world_the_end",
    }.get(dimension, dimension.removeprefix("minecraft:"))

    views: list[tuple[str, float, float]] = [("front-right", 1.0, 1.0), ("back-left", -1.0, -1.0)]
    result: list[tuple[str, tuple[str, float, float, float, float, float]]] = []
    for name, x_sign, z_sign in views:
        camera_x = target[0] + x_sign * distance
        camera_z = target[2] + z_sign * distance
        camera_y = target[1] + max(4.0, extent * 0.55)
        dx = target[0] - camera_x
        dy = target[1] - camera_y
        dz = target[2] - camera_z
        yaw = math.degrees(math.atan2(-dx, dz))
        pitch = -math.degrees(math.atan2(dy, math.hypot(dx, dz)))
        result.append((name, (world, camera_x, camera_y, camera_z, yaw, pitch)))
    return result


def _wait_for_new_png(render_dir: Path, before: dict[str, tuple[int, int]], timeout: int) -> Path | None:
    """Wait for a new, stable plugin render without touching old captures."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = []
        for path in render_dir.glob("*.png"):
            try:
                stat = path.stat()
            except OSError:
                continue
            previous = before.get(path.name)
            if previous is None or (stat.st_mtime_ns, stat.st_size) != previous:
                candidates.append((stat.st_mtime_ns, path))
        if candidates:
            candidate = max(candidates, key=lambda item: item[0])[1]
            try:
                size = candidate.stat().st_size
                time.sleep(0.15)
                if candidate.stat().st_size == size and size > 0:
                    return candidate
            except OSError:
                pass
        time.sleep(0.2)
    return None


class GodBridge:
    def __init__(self, config: BridgeConfig, rcon: Any | None = None) -> None:
        self.config = config
        self.rcon = rcon or RconClient(config.rcon_host, config.rcon_port, config.rcon_password)
        self._lock = threading.RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=100)
        self._chat: deque[dict[str, Any]] = deque(maxlen=50)
        configured_state = self._absolute_path(config.state_file)
        self._owner_id = self._normalize_owner_id(config.god_player_id)
        if config.require_god_scope and not self._owner_id:
            raise ValueError("MINECRAFT_REQUIRE_GOD_SCOPE is enabled but no player UUID was provided")
        self._profile_dir = self._absolute_path(config.profile_dir)
        if self._owner_id:
            self._state_path = self._profile_dir / self._profile_key(self._owner_id) / "god_state.json"
        else:
            self._state_path = configured_state
        self._events_path = self._absolute_path(config.events_file) if config.events_file else self._state_path.with_name("events.jsonl")
        ledger_path = self._absolute_path(config.ledger_file) if config.ledger_file else configured_state.with_name("god_ledger.sqlite3")
        self._ledger = GodLedger(ledger_path)
        self._next_event_id = 1
        self._god_state = self._load_state()
        self._refresh_events()

    @staticmethod
    def _normalize_owner_id(value: str) -> str:
        value = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27,}", value) or re.fullmatch(r"[0-9a-f]{32}", value):
            return value
        return ""

    @staticmethod
    def _profile_key(player_id: str) -> str:
        return re.sub(r"[^a-z0-9-]", "", player_id.casefold())[:64]

    def _absolute_path(self, value: str | Path | None) -> Path:
        if value is None:
            return Path("")
        path = Path(value)
        return path if path.is_absolute() else Path(__file__).resolve().parents[1] / path

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def _require_owner(self) -> str:
        if not self._owner_id:
            raise PermissionError("this bridge is not scoped to a player god")
        return self._owner_id

    def _owner_profile(self) -> dict[str, Any]:
        profile = self._ledger.player(self._owner_id) if self._owner_id else None
        name = str((profile or {}).get("name") or self.config.god_player_name or self.config.player_name)
        return {"player_id": self._owner_id, "name": name, "online": bool((profile or {}).get("online", False))}

    def _resolve_player(self, reference: str) -> dict[str, Any]:
        profile = self._ledger.player(reference)
        if not profile:
            raise ValueError(f"unknown player profile: {reference}")
        return profile

    def owned_player(self, reference: str = "") -> dict[str, Any]:
        owner_id = self._require_owner()
        profile = self._resolve_player(reference or owner_id)
        if profile["player_id"] != owner_id:
            raise PermissionError("a god may only target its own player")
        return profile

    def _load_state(self) -> dict[str, Any]:
        return self._load_state_from(self._state_path)

    @staticmethod
    def _load_state_from(path: Path) -> dict[str, Any]:
        if not path.exists():
            return json.loads(json.dumps(DEFAULT_GOD_STATE))
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return json.loads(json.dumps(DEFAULT_GOD_STATE))
        state = json.loads(json.dumps(DEFAULT_GOD_STATE))
        if isinstance(loaded, dict):
            state.update(loaded)
        return state

    def _state_path_for(self, player_id: str) -> Path:
        if self._owner_id and player_id == self._owner_id:
            return self._state_path
        return self._profile_dir / self._profile_key(player_id) / "god_state.json"

    def _load_state_for(self, player_id: str) -> dict[str, Any]:
        return self._load_state_from(self._state_path_for(player_id))

    def _save_state(self) -> None:
        self._save_state_to(self._state_path, self._god_state)

    @staticmethod
    def _save_state_to(path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _event_player_id(self, event: dict[str, Any], key: str = "player_uuid") -> str:
        value = str(event.get(key, "") or "").strip().lower()
        if value:
            return value
        name_key = {"player_uuid": "player", "victim_uuid": "victim", "killer_uuid": "killer"}.get(key, "player")
        name = str(event.get(name_key, "") or "")
        profile = self._ledger.player(name)
        return str(profile.get("player_id", "")) if profile else ""

    def event_matches_owner(self, event: dict[str, Any]) -> bool:
        """Return whether an event belongs in this god's private context."""
        if not self._owner_id:
            return True
        fields = ("player_uuid", "victim_uuid", "killer_uuid", "target_player_id", "from_player_id", "to_player_id")
        if any(str(event.get(field, "")).lower() == self._owner_id for field in fields):
            return True
        owner_name = self.current_player_name().casefold()
        return any(str(event.get(field, "")).casefold() == owner_name for field in ("player", "victim", "killer", "target"))

    def record_event(self, event: dict[str, Any]) -> dict[str, Any]:
        clean = dict(event)
        clean.setdefault("type", "unknown")
        clean.setdefault("at", utc_now())
        for id_key, name_key in (("player_uuid", "player"), ("victim_uuid", "victim"), ("killer_uuid", "killer")):
            player_id = str(clean.get(id_key, "") or "").strip().lower()
            player_name = str(clean.get(name_key, "") or "").strip()
            if player_id and player_name:
                try:
                    self._ledger.register_player(player_id, player_name, online=clean["type"] != "player_quit")
                except ValueError:
                    pass
        automatic_consequence = False
        consequence_level = 0
        consequence_player_id = ""
        consequence_state = self._god_state
        consequence_path = self._state_path
        if clean.get("type") in {"chat", "player_chat"} and isinstance(clean.get("message"), str):
            player = str(clean.get("player", ""))
            player_id = self._event_player_id(clean)
            is_owner_event = bool(player_id and player_id == self._owner_id) or (not self._owner_id and player.casefold() == self.current_player_name().casefold())
            if player and (not self._owner_id or is_owner_event):
                conduct = assess_conduct(clean["message"])
                clean["conduct"] = conduct
                now = time.time()
                with self._lock:
                    consequence_player_id = player_id or self._owner_id
                    if consequence_player_id and consequence_player_id != self._owner_id:
                        consequence_state = self._load_state_for(consequence_player_id)
                        consequence_path = self._state_path_for(consequence_player_id)
                    window_started = _parse_epoch(consequence_state.get("conduct_window_started_at"))
                    if window_started is None or now - window_started > 180:
                        consequence_state["conduct_score"] = 0
                        window_started = now
                    score = int(consequence_state.get("conduct_score", 0)) + int(conduct["points"])
                    consequence_state["conduct_score"] = score
                    consequence_state["conduct_window_started_at"] = _epoch_string(window_started)
                    last_consequence = _parse_epoch(consequence_state.get("last_consequence_at"))
                    if score >= 3 and (last_consequence is None or now - last_consequence >= 45):
                        automatic_consequence = True
                        consequence_level = int(consequence_state.get("consequence_count", 0)) + 1
                        consequence_state["conduct_score"] = 0
                        consequence_state["last_consequence_at"] = _epoch_string(now)
                        consequence_state["consequence_count"] = consequence_level
                    self._save_state_to(consequence_path, consequence_state)
                    if consequence_path == self._state_path:
                        self._god_state = consequence_state
                clean["conduct"]["automatic_consequence"] = automatic_consequence
                if automatic_consequence:
                    clean["conduct"]["consequence_level"] = consequence_level
        with self._lock:
            supplied_event_id = clean.pop("event_id", None)
            if supplied_event_id is not None:
                clean["source_event_id"] = supplied_event_id
            clean["event_id"] = self._ledger.next_event_id(self._next_event_id - 1)
            self._next_event_id = max(self._next_event_id, int(clean["event_id"]) + 1)
            self._events.append(clean)
            if clean["type"] in {"chat", "player_chat"} or "message" in clean:
                self._chat.append(clean)
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            with self._events_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(clean, separators=(",", ":")) + "\n")
        if automatic_consequence and not self.config.dry_run:
            self._apply_consequence(consequence_level, consequence_player_id)
        return clean

    def _apply_consequence(self, level: int, player_id: str = "") -> dict[str, Any]:
        """Apply an immediate consequence when sustained abuse crosses the threshold."""
        player = self.current_player_name() if not player_id or player_id == self._owner_id else str((self._ledger.player(player_id) or {}).get("name", ""))
        if not player:
            return {"ok": False, "error": "consequence target is not registered"}
        result = self._execute_targeted(f"execute at {player} run summon minecraft:lightning_bolt ~ ~ ~", player_id or self._owner_id)
        result["action"] = "automatic_consequence"
        result["consequence_level"] = level
        state_path = self._state_path_for(player_id or self._owner_id)
        state = self._god_state if not player_id or player_id == self._owner_id else self._load_state_for(player_id)
        state["last_major_intervention"] = f"Automatic consequence level {level}: lightning answered sustained blasphemy."
        self._save_state_to(state_path, state)
        if state_path == self._state_path:
            self._god_state = state
        return result

    def _refresh_events(self) -> None:
        if not self._events_path.exists():
            return
        try:
            rows = self._events_path.read_text(encoding="utf-8").splitlines()[-100:]
        except OSError:
            return
        with self._lock:
            self._events.clear()
            self._chat.clear()
            for row in rows:
                try:
                    event = json.loads(row)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_id = int(event.get("event_id", self._next_event_id))
                self._next_event_id = max(self._next_event_id, event_id + 1)
                self._events.append(event)
                if event.get("type") in {"chat", "player_chat"} or "message" in event:
                    self._chat.append(event)

    def _assert_command_authorized(self, command: str, *, target_player_id: str = "", capability: bool = False, allow_shared: bool = False) -> None:
        if not self._owner_id or capability or allow_shared:
            return
        if target_player_id and target_player_id != self._owner_id:
            raise PermissionError("a god may only affect its own player unless it consumes an approved intervention")
        lowered = command.casefold()
        if re.search(r"@[apr]\b", lowered) or re.search(r"@e(?!\[tag=god_[a-z0-9_.-]+)", lowered):
            raise PermissionError("player selectors that can affect another player are not available to a player god")
        for profile in self._ledger.players():
            name = str(profile.get("name", ""))
            if profile.get("player_id") != self._owner_id and re.search(rf"(?<![A-Za-z0-9_]){re.escape(name.casefold())}(?![A-Za-z0-9_])", lowered):
                raise PermissionError(f"command targets another player's god-owned subject: {name}")

    def _execute_targeted(self, command: str, target_player_id: str, *, capability: bool = False) -> dict[str, Any]:
        self._assert_command_authorized(command, target_player_id=target_player_id, capability=capability)
        return self.execute(command, record=True, target_player_id=target_player_id, capability=capability)

    def execute(self, command: str, *, record: bool = True, target_player_id: str = "", capability: bool = False, allow_shared: bool = False) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if len(command) > self.config.max_command_length:
            raise ValueError(f"command exceeds {self.config.max_command_length} characters")
        if any(char in command for char in "\r\n\x00"):
            raise ValueError("command cannot contain newlines or NUL bytes")
        command = command.strip().lstrip("/")
        self._assert_command_authorized(command, target_player_id=target_player_id, capability=capability, allow_shared=allow_shared)
        if self.config.dry_run:
            result = {"command": command, "ok": True, "dry_run": True, "output": ""}
        else:
            response = self.rcon.command(command)
            output = response.body
            result = {
                "command": command,
                "ok": not looks_like_command_failure(output),
                "dry_run": False,
                "output": output,
            }
            if not result["ok"]:
                result["error"] = "Minecraft rejected the command or found no matching target."
        if record:
            action_event: dict[str, Any] = {"type": "god_action", "command": command, "result": result.get("output", "")}
            if self._owner_id:
                action_event.update({"player_uuid": self._owner_id, "player": self.current_player_name()})
            if target_player_id:
                action_event["target_player_id"] = target_player_id
            self.record_event(action_event)
        return result

    def command_at_player(self, command: str) -> str:
        """Run a command with the active player as both executor and position anchor.

        RCON commands execute from the server console.  In particular, ``~`` in a
        console command is not relative to the player, which made the old tool
        descriptions actively misleading for scene work.
        """
        player = self.current_player_name()
        return f"execute as {player} at @s run {command}"

    def inspect_area(self, radius: int = 8, vertical_radius: int = 5) -> dict[str, Any]:
        """Return the Paper plugin's exact local scene scan, if available."""
        radius = max(1, min(int(radius), 12))
        vertical_radius = max(1, min(int(vertical_radius), 8))
        result = self.execute(
            f"godscan {self.current_player_name()} {radius} {vertical_radius}",
            record=False,
        )
        if not result["ok"]:
            return {"ok": False, "error": result.get("output", "")}
        try:
            scan = json.loads(result.get("output", ""))
        except json.JSONDecodeError:
            return {"ok": False, "error": "godscan returned non-JSON output", "output": result.get("output", "")}
        if isinstance(scan, dict) and scan.get("error"):
            return {"ok": False, "error": scan.get("error"), "scan": scan}
        return {"ok": True, "scan": scan}

    def inspect_block(self, position: str) -> dict[str, Any]:
        """Inspect one exact block in the tracked player's current dimension."""
        x, y, z = self.resolve_block_position(position)
        result = self.execute(f"godcheck {self.current_player_name()} {x} {y} {z}", record=False)
        if not result["ok"]:
            return {"ok": False, "error": result.get("output", "godcheck failed"), "position": [x, y, z]}
        try:
            block = json.loads(result.get("output", ""))
        except json.JSONDecodeError:
            return {"ok": False, "error": "godcheck returned non-JSON output", "output": result.get("output", "")}
        return {"ok": True, "block": block}

    def capture_scene(
        self,
        radius: int = 12,
        vertical_radius: int = 8,
        width: int = 900,
        height: int = 650,
    ) -> dict[str, Any]:
        """Capture private, textured views from the live Paper camera plugin.

        SpectatorCamera owns an invisible virtual camera and a server-side
        renderer. The bridge only points it at the observed scene and copies
        the resulting PNGs into the MCP capture directory; the tracked player
        is never moved, used as a camera, or notified.
        """
        scene = self.inspect_area(radius, vertical_radius)
        if not scene.get("ok"):
            return {"ok": False, "error": "Could not capture because the live scene scan failed.", "scene": scene}

        workspace = Path(__file__).resolve().parents[1]
        render_dir = Path(self.config.camera_plugin_render_dir)
        if not render_dir.is_absolute():
            render_dir = workspace / render_dir
        output_dir = Path(self.config.camera_output_dir)
        if not output_dir.is_absolute():
            output_dir = workspace / output_dir
        if not render_dir.exists():
            return {"ok": False, "error": f"SpectatorCamera render directory not found: {render_dir}. Install the plugin and restart Paper.", "scene": scene["scan"]}

        scan = scene["scan"]
        camera_points = _camera_points(scan)
        timeout = max(10, min(int(self.config.camera_timeout), 300))
        output_dir.mkdir(parents=True, exist_ok=True)
        images: list[dict[str, Any]] = []

        # Start once if needed, force manual mode, then take two complementary
        # views. RCON is the console, so these commands do not appear in chat.
        started = self.execute("camera start", record=False)
        if not started["ok"] and "already" not in started.get("output", "").lower():
            return {"ok": False, "error": f"SpectatorCamera could not start: {started.get('output', '')}", "scene": scan}
        manual = self.execute("camera auto off", record=False)
        if not manual["ok"]:
            return {"ok": False, "error": f"SpectatorCamera could not enter manual mode: {manual.get('output', '')}", "scene": scan}

        for view_name, point in camera_points:
            before = {path.name: (path.stat().st_mtime_ns, path.stat().st_size) for path in render_dir.glob("*.png")}
            world, x, y, z, yaw, pitch = point
            point_result = self.execute(
                f"camera point {world} {_format_number(x)} {_format_number(y)} {_format_number(z)} {_format_number(yaw)} {_format_number(pitch)}",
                record=False,
            )
            if not point_result["ok"]:
                return {"ok": False, "error": f"SpectatorCamera could not position the {view_name} view: {point_result.get('output', '')}", "scene": scan}
            render_result = self.execute("camera render", record=False)
            if not render_result["ok"]:
                return {"ok": False, "error": f"SpectatorCamera render failed for {view_name}: {render_result.get('output', '')}", "scene": scan}
            rendered_path = _wait_for_new_png(render_dir, before, timeout)
            if rendered_path is None:
                return {"ok": False, "error": f"SpectatorCamera timed out while rendering the {view_name} view.", "scene": scan}
            destination = output_dir / f"scene-{int(time.time() * 1000)}-{view_name}.png"
            destination.write_bytes(rendered_path.read_bytes())
            images.append({"view": view_name, "path": str(destination), "camera": {"world": world, "x": x, "y": y, "z": z, "yaw": yaw, "pitch": pitch}})

        return {"ok": True, "renderer": "SpectatorCamera", "scene": scan, "images": images}

    def resolve_block_position(self, position: str) -> tuple[int, int, int]:
        """Resolve a block coordinate, including ``~`` offsets, like Minecraft."""
        tokens = parse_position(position).split()
        if not any(token.startswith("~") for token in tokens):
            return tuple(int(float(token)) for token in tokens)  # type: ignore[return-value]
        bases = self._player_position()
        return tuple(_resolve_coordinate(token, base, block=True) for token, base in zip(tokens, bases))  # type: ignore[return-value]

    def _player_position(self) -> tuple[float, float, float]:
        result = self.execute(f"data get entity {self.current_player_name()} Pos", record=False)
        if not result["ok"]:
            raise RuntimeError(result.get("output", "Could not read player position"))
        values = re.findall(r"(-?\d+(?:\.\d+)?)d", result.get("output", ""))
        if len(values) < 3:
            raise RuntimeError(f"Could not parse player position: {result.get('output', '')}")
        return tuple(float(value) for value in values[-3:])  # type: ignore[return-value]

    def current_player_id(self) -> str:
        if self._owner_id:
            return self._owner_id
        active_name = ""
        active_file = Path(self.config.active_player_file)
        try:
            active_name = active_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        profile = self._ledger.player(active_name or self.config.player_name)
        return str(profile.get("player_id", "")) if profile else ""

    def current_player_name(self) -> str:
        if self._owner_id:
            return self._owner_profile()["name"]
        active_file = Path(self.config.active_player_file)
        try:
            active_name = active_file.read_text(encoding="utf-8").strip()
        except OSError:
            active_name = ""
        if re.fullmatch(r"[A-Za-z0-9_]{1,16}", active_name):
            return active_name
        return self.config.player_name

    def register_player(self, player_id: str, name: str, *, online: bool = True) -> dict[str, Any]:
        return self._ledger.register_player(player_id, name, online=online)

    def player_profiles(self) -> list[dict[str, Any]]:
        return self._ledger.players()

    def observe_world(self) -> dict[str, Any]:
        player_name = self.current_player_name()
        observation: dict[str, Any] = {
            "observed_at": utc_now(),
            "player": player_name,
            "god_state": self.get_god_state(),
            "recent_events": self.recent_events(8),
            "recent_chat": self.read_chat(8),
        }
        if self.config.dry_run:
            observation["connection"] = "dry_run"
            return observation

        queries = {
            "position": f"data get entity {player_name} Pos",
            "health": f"data get entity {player_name} Health",
            "food": f"data get entity {player_name} foodLevel",
            "dimension": f"data get entity {player_name} Dimension",
        }
        observation["connection"] = "rcon"
        observation["queries"] = {}
        for name, command in queries.items():
            try:
                observation["queries"][name] = self.execute(command, record=False)["output"]
            except Exception as exc:  # observation should degrade gracefully
                observation["queries"][name] = {"error": str(exc)}
        # Keep the high-level observation useful on its own.  Older plugin
        # installations may not have /godscan yet, so this is deliberately
        # additive and degrades to a clear capability error.
        try:
            observation["scene"] = self.inspect_area()
        except Exception as exc:
            observation["scene"] = {"ok": False, "error": str(exc)}
        return observation

    def read_chat(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        self._refresh_events()
        with self._lock:
            return [event for event in self._chat if self.event_matches_owner(event)][-limit:]

    def recent_events(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        self._refresh_events()
        with self._lock:
            return [event for event in self._events if self.event_matches_owner(event)][-limit:]

    def events_since(self, after_event_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self._refresh_events()
        with self._lock:
            return [event for event in self._events if int(event.get("event_id", 0)) > after_event_id and self.event_matches_owner(event)][:max(1, min(limit, 200))]

    def latest_event_id(self) -> int:
        self._refresh_events()
        with self._lock:
            return self._next_event_id - 1

    def get_god_state(self) -> dict[str, Any]:
        with self._lock:
            # The event hub may update conduct in the same profile while the
            # god process is thinking. Reload before exposing or modifying it
            # so a narrative update cannot silently erase a fresh consequence.
            self._god_state = self._load_state_from(self._state_path)
            return json.loads(json.dumps(self._god_state))

    def update_god_state(self, updates: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(updates, dict):
            raise ValueError("updates must be an object")
        allowed = set(DEFAULT_GOD_STATE)
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unknown god state fields: {', '.join(sorted(unknown))}")
        with self._lock:
            self._god_state = self._load_state_from(self._state_path)
            self._god_state.update(updates)
            self._save_state()
            return self.get_god_state()

    def god_say(self, message: str) -> dict[str, Any]:
        """Backward-compatible private speech to this god's own player."""
        return self.god_whisper(message)

    def god_broadcast(self, message: str) -> dict[str, Any]:
        """Make an explicitly public, attributed pantheon announcement."""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if len(message) > 256:
            raise ValueError("god message cannot exceed 256 characters")
        speaker = f"[God of {self.current_player_name()}] "
        component = [
            {"text": speaker, "color": "dark_purple", "bold": True},
            {"text": message, "color": "light_purple"},
        ]
        tellraw = f"tellraw @a {json.dumps(component, separators=(',', ':'))}"
        result = self.execute(tellraw, allow_shared=True)
        result.update({"message": message, "speaker": speaker, "public": True})
        return result

    def god_whisper(self, message: str) -> dict[str, Any]:
        owner_id = self._owner_id
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if len(message) > 256:
            raise ValueError("god message cannot exceed 256 characters")
        player = self.current_player_name()
        tellraw = f"tellraw {player} {{\"text\":{json.dumps(message)},\"color\":\"dark_purple\",\"bold\":true}}"
        result = self._execute_targeted(tellraw, owner_id)
        result.update({"message": message, "player": player, "private": True})
        return result

    def send_god_message(self, target: str, message: str, *, subject: str = "", event_id: int | None = None) -> dict[str, Any]:
        owner_id = self._require_owner()
        target_profile = self._resolve_player(target)
        if target_profile["player_id"] == owner_id:
            raise ValueError("use whisper_player to speak to your own player")
        if not isinstance(message, str) or not message.strip() or len(message) > 1000:
            raise ValueError("message must be between 1 and 1000 characters")
        interaction = self._ledger.add_interaction(
            kind="god_message",
            from_player_id=owner_id,
            to_player_id=target_profile["player_id"],
            status="delivered",
            message=message.strip(),
            reason=subject.strip()[:256],
            event_id=event_id,
        )
        self.record_event({
            "type": "god_message",
            "from_player_id": owner_id,
            "from_player": self.current_player_name(),
            "to_player_id": target_profile["player_id"],
            "to_player": target_profile["name"],
            "message": message.strip(),
            "subject": subject.strip()[:256],
            "interaction_id": interaction["id"],
            "source_event_id": event_id,
        })
        return {"ok": True, "interaction": interaction}

    def list_interventions(self, limit: int = 20) -> list[dict[str, Any]]:
        owner_id = self._require_owner()
        return self._ledger.interactions_for(owner_id, limit=limit)

    def request_intervention(self, target: str, reason: str, *, event_id: int | None = None, duration: int = 900) -> dict[str, Any]:
        owner_id = self._require_owner()
        target_profile = self._resolve_player(target)
        if target_profile["player_id"] == owner_id:
            raise ValueError("a god does not need permission to affect its own player")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("reason must be between 1 and 1000 characters")
        interaction = self._ledger.add_interaction(
            kind="intervention_request",
            from_player_id=owner_id,
            to_player_id=target_profile["player_id"],
            action="smite",
            target_player_id=target_profile["player_id"],
            reason=reason.strip(),
            event_id=event_id,
            expires_at=expiry_after(max(60, min(int(duration), 3600))),
        )
        self.record_event({
            "type": "intervention_request",
            "interaction_id": interaction["id"],
            "from_player_id": owner_id,
            "from_player": self.current_player_name(),
            "to_player_id": target_profile["player_id"],
            "to_player": target_profile["name"],
            "target_player_id": target_profile["player_id"],
            "action": "smite",
            "reason": reason.strip(),
        })
        return {"ok": True, "request": interaction}

    def respond_intervention(self, request_id: str, decision: str, note: str = "") -> dict[str, Any]:
        owner_id = self._require_owner()
        result = self._ledger.respond(request_id, owner_id, decision, str(note)[:1000])
        self.record_event({
            "type": "intervention_response",
            "interaction_id": request_id,
            "from_player_id": owner_id,
            "to_player_id": result["from_player_id"],
            "status": result["status"],
            "message": result.get("message", ""),
        })
        return {"ok": True, "request": result}

    def use_intervention(self, request_id: str) -> dict[str, Any]:
        owner_id = self._require_owner()
        interaction = self._ledger.consume(request_id, owner_id)
        target = self._resolve_player(interaction["target_player_id"])
        command = f"execute at {target['name']} run summon minecraft:lightning_bolt ~ ~ ~"
        result = self._execute_targeted(command, target["player_id"], capability=True)
        result.update({"action": "approved_smite", "request_id": request_id, "target": target["name"]})
        self.record_event({
            "type": "intervention_used",
            "interaction_id": request_id,
            "player_uuid": owner_id,
            "target_player_id": target["player_id"],
            "target": target["name"],
            "result": result.get("output", ""),
        })
        return result

    def punish_player(self, mode: str = "smite") -> dict[str, Any]:
        """Deliver an intentional consequence chosen by the god."""
        player = self.current_player_name()
        if mode not in {"smite", "storm"}:
            raise ValueError("mode must be smite or storm")
        command = (
            f"execute at {player} run summon minecraft:lightning_bolt ~ ~ ~"
            if mode == "smite" else "weather thunder 120"
        )
        result = self.action("punish_player", lambda: command)
        if result.get("ok"):
            with self._lock:
                self._god_state = self._load_state_from(self._state_path)
                self._god_state["last_major_intervention"] = f"Delivered deliberate {mode} consequence to {player}."
                self._save_state()
        return result

    def action(self, name: str, command_builder: Callable[[], str]) -> dict[str, Any]:
        command = command_builder()
        result = self.execute(command)
        result["action"] = name
        return result

    def structure_library(self) -> StructureLibrary:
        return StructureLibrary(
            Path(__file__).resolve().parents[1],
            catalog_file=self.config.structure_catalog_file,
            datapacks_dir=self.config.structure_datapacks_dir,
            download_dir=self.config.structure_download_dir,
            max_download_mb=self.config.structure_max_download_mb,
            game_version=self.config.minecraft_version,
        )

    def list_structures(self, query: str = "") -> dict[str, Any]:
        return {"ok": True, "structures": self.structure_library().list(query)}

    def search_structures(self, query: str, limit: int = 8) -> dict[str, Any]:
        return self.structure_library().search_online(query, limit)

    def import_structure(self, project_id: str, version_id: str = "", namespace: str = "") -> dict[str, Any]:
        if self.config.dry_run:
            return {"ok": True, "dry_run": True, "project_id": project_id, "version_id": version_id, "structures": []}
        result = self.structure_library().import_modrinth(project_id, version_id, namespace)
        enable = self.execute(f'datapack enable "file/{result["pack_name"]}"', record=False)
        reload_result = self.execute("reload", record=False) if enable["ok"] else {"ok": False, "error": "datapack was not enabled"}
        result["activation"] = {"enable": enable, "reload": reload_result}
        result["ok"] = bool(result.get("ok") and enable.get("ok") and reload_result.get("ok"))
        return result

    def place_structure(self, structure: str, position: str, rotation: str = "none") -> dict[str, Any]:
        if rotation not in {"none", "clockwise_90", "clockwise_180", "counterclockwise_90"}:
            raise ValueError("invalid structure rotation")
        entry = self.structure_library().get(structure)
        if self.config.dry_run and "~" in position:
            block_position = (0, 0, 0)
        else:
            block_position = self.resolve_block_position(position)
        size = entry.get("size", []) if entry else []
        preflight = self._structure_preflight(block_position, size) if size and not self.config.dry_run else {"ok": None, "status": "not-run"}
        if preflight.get("ok") is False:
            return {
                "ok": False,
                "action": "place_structure",
                "structure": structure,
                "origin": {"x": block_position[0], "y": block_position[1], "z": block_position[2]},
                "preflight": preflight,
                "error": "Structure placement refused because the inspected footprint is unsafe or occupied.",
            }
        command_position = parse_position(position)
        command = f"place template {structure} {command_position} {rotation}"
        if "~" in command_position:
            command = self.command_at_player(command)
        result = self.action("place_structure", lambda: command)
        result["structure"] = structure
        result["origin"] = None if self.config.dry_run and "~" in position else {"x": block_position[0], "y": block_position[1], "z": block_position[2]}
        result["preflight"] = preflight
        if not result.get("ok") or not entry or self.config.dry_run:
            if not entry:
                result["verification"] = {"ok": None, "status": "command-confirmed-only", "reason": "No imported metadata is available for this template."}
            elif self.config.dry_run:
                result["verification"] = {"ok": None, "status": "dry-run", "reason": "World verification is unavailable in dry-run mode."}
            return result

        samples = []
        for sample in entry.get("samples", []):
            raw_pos = sample.get("position") if isinstance(sample, dict) else None
            block_name = sample.get("block") if isinstance(sample, dict) else ""
            if not isinstance(raw_pos, list) or len(raw_pos) != 3 or not isinstance(block_name, str):
                continue
            rotated = _rotate_structure_position(raw_pos, size, rotation)
            target = tuple(block_position[index] + rotated[index] for index in range(3))
            check = self.execute(f"data get block {target[0]} {target[1]} {target[2]}", record=False)
            samples.append({"position": target, "expected": block_name, "result": check, "matched": block_name in check.get("output", "")})
        result["size"] = size
        result["verification"] = {"ok": bool(samples) and all(item["matched"] for item in samples), "samples": samples}
        result["ok"] = bool(result["ok"] and result["verification"]["ok"])
        return result

    def _structure_preflight(self, origin: tuple[int, int, int], size: list[Any]) -> dict[str, Any]:
        """Check representative footprint/clearance points before mutating terrain."""
        if len(size) < 3:
            return {"ok": None, "status": "insufficient-metadata"}
        sx, sy, sz = (max(1, int(size[0])), max(1, int(size[1])), max(1, int(size[2])))
        x, y, z = origin
        points = {
            (x, y, z),
            (x + sx - 1, y, z),
            (x, y, z + sz - 1),
            (x + sx - 1, y, z + sz - 1),
            (x + sx // 2, y, z + sz // 2),
            (x + sx // 2, y + min(1, sy - 1), z + sz // 2),
            (x + sx // 2, y + min(3, sy - 1), z + sz // 2),
            (x + sx // 2, y + sy - 1, z + sz // 2),
        }
        checks: list[dict[str, Any]] = []
        for point in sorted(points):
            inspected = self.inspect_block(f"{point[0]} {point[1]} {point[2]}")
            checks.append({"position": point, "result": inspected})
            if not inspected.get("ok"):
                return {"ok": False, "reason": "exact block inspection failed", "checks": checks}
            block = inspected.get("block", {})
            if block.get("liquid"):
                return {"ok": False, "reason": "structure footprint intersects liquid", "checks": checks}
            if point[1] > y and block.get("solid") and not block.get("replaceable"):
                return {"ok": False, "reason": "structure clearance intersects an occupied solid block", "checks": checks}
        return {"ok": True, "checks": checks}


def _bounded_limit(value: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(value, 50))


def parse_position(position: str) -> str:
    if not isinstance(position, str) or not position.strip():
        raise ValueError("position must be a Minecraft coordinate string, such as '~ ~ ~'")
    if len(position) > 100 or not re.fullmatch(r"[~\-+\d\.\s]+", position.strip()) or len(position.split()) != 3:
        raise ValueError("position contains unsupported characters")
    return position.strip()


def looks_like_command_failure(output: str) -> bool:
    """Recognize Minecraft's textual command failures returned over RCON."""
    if not output:
        return False
    lowered = output.lower()
    return any(
        marker in lowered
        for marker in (
            "expected literal",
            "unknown or incomplete command",
            "unknown command",
            "incorrect argument",
            "no entity was found",
            "no player was found",
            "cannot find",
            "failed to",
            "invalid argument",
            "permission",
        )
    )


def _resolve_coordinate(token: str, base: float, *, block: bool) -> int | float:
    if token.startswith("~"):
        value = base + (float(token[1:]) if token[1:] else 0.0)
    else:
        value = float(token)
    return int(value // 1) if block else value


def _rotate_structure_position(position: list[Any], size: list[Any], rotation: str) -> tuple[int, int, int]:
    """Rotate a structure-local block position around its template origin."""
    x, y, z = (int(position[0]), int(position[1]), int(position[2]))
    sx = int(size[0]) if len(size) > 0 else 0
    sz = int(size[2]) if len(size) > 2 else 0
    if rotation == "clockwise_90":
        return sz - 1 - z, y, x
    if rotation == "clockwise_180":
        return sx - 1 - x, y, sz - 1 - z
    if rotation == "counterclockwise_90":
        return z, y, sx - 1 - x
    return x, y, z


def parse_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:.\-/]+", value):
        raise ValueError(f"{field} must be a Minecraft identifier")
    return value
