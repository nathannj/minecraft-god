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
    active_player_file: str = "state/active_player.txt"
    event_host: str = "127.0.0.1"
    event_port: int = 8765
    event_token: str = ""
    state_file: str = "state/god_state.json"
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
        self._state_path = Path(config.state_file)
        self._events_path = self._state_path.with_name("events.jsonl")
        self._next_event_id = 1
        self._god_state = self._load_state()
        self._refresh_events()

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return json.loads(json.dumps(DEFAULT_GOD_STATE))
        try:
            loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return json.loads(json.dumps(DEFAULT_GOD_STATE))
        state = json.loads(json.dumps(DEFAULT_GOD_STATE))
        if isinstance(loaded, dict):
            state.update(loaded)
        return state

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(self._god_state, indent=2) + "\n", encoding="utf-8")

    def record_event(self, event: dict[str, Any]) -> dict[str, Any]:
        clean = dict(event)
        clean.setdefault("type", "unknown")
        clean.setdefault("at", utc_now())
        automatic_consequence = False
        consequence_level = 0
        if clean.get("type") in {"chat", "player_chat"} and isinstance(clean.get("message"), str):
            player = str(clean.get("player", ""))
            if player and player.casefold() == self.current_player_name().casefold():
                conduct = assess_conduct(clean["message"])
                clean["conduct"] = conduct
                now = time.time()
                with self._lock:
                    window_started = _parse_epoch(self._god_state.get("conduct_window_started_at"))
                    if window_started is None or now - window_started > 180:
                        self._god_state["conduct_score"] = 0
                        window_started = now
                    score = int(self._god_state.get("conduct_score", 0)) + int(conduct["points"])
                    self._god_state["conduct_score"] = score
                    self._god_state["conduct_window_started_at"] = _epoch_string(window_started)
                    last_consequence = _parse_epoch(self._god_state.get("last_consequence_at"))
                    if score >= 3 and (last_consequence is None or now - last_consequence >= 45):
                        automatic_consequence = True
                        consequence_level = int(self._god_state.get("consequence_count", 0)) + 1
                        self._god_state["conduct_score"] = 0
                        self._god_state["last_consequence_at"] = _epoch_string(now)
                        self._god_state["consequence_count"] = consequence_level
                    self._save_state()
                clean["conduct"]["automatic_consequence"] = automatic_consequence
                if automatic_consequence:
                    clean["conduct"]["consequence_level"] = consequence_level
        with self._lock:
            clean.setdefault("event_id", self._next_event_id)
            self._next_event_id = max(self._next_event_id, int(clean["event_id"]) + 1)
            self._events.append(clean)
            if clean["type"] in {"chat", "player_chat"} or "message" in clean:
                self._chat.append(clean)
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            with self._events_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(clean, separators=(",", ":")) + "\n")
        if automatic_consequence and not self.config.dry_run:
            self._apply_consequence(consequence_level)
        return clean

    def _apply_consequence(self, level: int) -> dict[str, Any]:
        """Apply an immediate consequence when sustained abuse crosses the threshold."""
        player = self.current_player_name()
        result = self.execute(f"execute at {player} run summon minecraft:lightning_bolt ~ ~ ~")
        result["action"] = "automatic_consequence"
        result["consequence_level"] = level
        with self._lock:
            self._god_state["last_major_intervention"] = f"Automatic consequence level {level}: lightning answered sustained blasphemy."
            self._save_state()
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

    def execute(self, command: str, *, record: bool = True) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if len(command) > self.config.max_command_length:
            raise ValueError(f"command exceeds {self.config.max_command_length} characters")
        if any(char in command for char in "\r\n\x00"):
            raise ValueError("command cannot contain newlines or NUL bytes")
        command = command.strip().lstrip("/")
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
            self.record_event({"type": "god_action", "command": command, "result": result.get("output", "")})
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

    def current_player_name(self) -> str:
        active_file = Path(self.config.active_player_file)
        try:
            active_name = active_file.read_text(encoding="utf-8").strip()
        except OSError:
            active_name = ""
        if re.fullmatch(r"[A-Za-z0-9_]{1,16}", active_name):
            return active_name
        return self.config.player_name

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
            return list(self._chat)[-limit:]

    def recent_events(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        self._refresh_events()
        with self._lock:
            return list(self._events)[-limit:]

    def events_since(self, after_event_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self._refresh_events()
        with self._lock:
            return [event for event in self._events if int(event.get("event_id", 0)) > after_event_id][:max(1, min(limit, 200))]

    def latest_event_id(self) -> int:
        self._refresh_events()
        with self._lock:
            return self._next_event_id - 1

    def get_god_state(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._god_state))

    def update_god_state(self, updates: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(updates, dict):
            raise ValueError("updates must be an object")
        allowed = set(DEFAULT_GOD_STATE)
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unknown god state fields: {', '.join(sorted(unknown))}")
        with self._lock:
            self._god_state.update(updates)
            self._save_state()
            return self.get_god_state()

    def god_say(self, message: str) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if len(message) > 256:
            raise ValueError("god message cannot exceed 256 characters")
        tellraw = f"tellraw @a {{\"text\":{json.dumps(message)},\"color\":\"dark_purple\",\"bold\":true}}"
        result = self.execute(tellraw)
        result["message"] = message
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
