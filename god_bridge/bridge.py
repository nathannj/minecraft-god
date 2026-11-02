"""State, event intake, and high-level god actions."""

from __future__ import annotations

import json
import difflib
import math
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .rcon import RconClient
from .ledger import GodLedger, expiry_after
from .story import DIRECT_GOD_ADDRESS, compact_event, validate_watch
from .structures import StructureLibrary
from .authority import authorize, check_world_bounds
from .state_lock import ProfileLock, profile_transaction


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_dimension(value: str) -> str:
    dimension = str(value or "minecraft:overworld").strip().casefold()
    aliases = {
        "overworld": "minecraft:overworld",
        "world": "minecraft:overworld",
        "nether": "minecraft:the_nether",
        "the_nether": "minecraft:the_nether",
        "world_nether": "minecraft:the_nether",
        "end": "minecraft:the_end",
        "the_end": "minecraft:the_end",
        "world_the_end": "minecraft:the_end",
    }
    dimension = aliases.get(dimension, dimension)
    if not re.fullmatch(r"[a-z0-9_.-]+:[a-z0-9_./-]+", dimension):
        raise ValueError("dimension must be a namespaced Minecraft dimension")
    return dimension


def absolute_block_position(value: str) -> tuple[int, int, int]:
    position = parse_position(value)
    if "~" in position:
        raise ValueError("remote positions must use absolute coordinates")
    return tuple(math.floor(float(token)) for token in position.split())  # type: ignore[return-value]


def landmark_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    if not key:
        raise ValueError("landmark name is required")
    return key[:64]


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
    insult = r"(?:fuck\w*|pussy|retard\w*|nonce|dipshit|bitch\w*|cunt\w*)"
    directed = bool(re.search(rf"\b(?:fuck\s+(?:you|off)|you(?:'re| are)?\s+(?:(?:a|an|the|such|fucking)\s+){{0,3}}{insult}|{insult}\s+(?:god|deity)|god\s+is\s+(?:(?:a|an|the)\s+)?{insult})\b", normalized))
    threat = bool(re.search(r"\b(?:kill|end|destroy|delete|hurt)\s+(?:you|your\s+(?:memory|existence))\b", normalized))
    points = (min(2, len(profanity)) if directed else 0) + (2 if threat else 0)
    if points == 0:
        return {"points": 0, "severity": "none", "reason": "ordinary speech"}
    severity = "severe" if points >= 3 else "abusive"
    return {"points": points, "severity": severity, "reason": "direct abuse or threat"}


@dataclass
class BridgeConfig:
    rcon_host: str = "127.0.0.1"
    rcon_port: int = 25575
    rcon_password: str = ""
    player_name: str = "Player"
    god_player_id: str = ""
    god_player_name: str = ""
    profile_dir: str = "state/players"
    active_player_file: str = "state/active_player.txt"
    turn_context_file: str = "state/turn_context.json"
    event_host: str = "127.0.0.1"
    event_port: int = 8765
    event_token: str = ""
    state_file: str = "state/god_state.json"
    events_file: str | None = None
    ledger_file: str | None = None
    max_gods: int = 2
    require_god_scope: bool = False
    dry_run: bool = False
    max_command_length: int = 512
    camera_output_dir: str = "state/captures"
    camera_plugin_render_dir: str = "plugins/SpectatorCamera/renders"
    camera_timeout: int = 120
    structure_catalog_file: str = "state/structure_catalog.json"
    structure_datapacks_dir: str = "world/datapacks"
    structure_download_dir: str = "state/structure_downloads"
    structure_max_download_mb: int = 64
    minecraft_version: str = "26.2"
    repeated_speech_cooldown: int = 600


DEFAULT_GOD_STATE = {
    "disposition": 0,
    "opinion_of_player": "",
    "judgment_basis": "",
    "preferred_treatment": "",
    "current_interest": "",
    "current_plan": "",
    "chaos_budget": "low",
    "last_major_intervention": "",
    "story_phase": "dormant",
    "active_thread": "",
    "player_choice": "",
    "story_watches": [],
    "story_objectives": [],
    "landmarks": {},
    "next_eligible_intervention_at": "",
    "last_player_visible_message": {},
    "last_spoken_turn_id": "",
    "last_world_impact": "",
    "unresolved_promises": [],
    "conduct_score": 0,
    "conduct_window_started_at": "",
    "last_consequence_at": "",
    "consequence_count": 0,
    "memories": [],
    "values": [],
    "judgment_style": "",
    "player_choice_history": [],
}

MAX_MEMORIES = 64
MAX_LANDMARKS = 64
MAX_OBJECTIVES = 24
WATCH_KINDS = {
    "event", "region", "item", "block_place", "block_break", "health_below",
    "mob_kill", "structure_change", "all", "any",
}

SIGN_COLORS = {
    "black", "blue", "green", "cyan", "red", "purple", "yellow", "white",
    "orange", "magenta", "lime", "light_blue", "pink", "gray", "light_gray", "brown",
}
MESSAGE_TYPES = {"intent", "report", "request", "warning", "verified_fact", "uncertain_claim", "approval"}


def _sign_line_width(text: str) -> int:
    narrow = set(" ilI!.,:;'|")
    wide = set("mwMW")
    return sum((2 if character in narrow else 7 if character in wide else 5) + 1 for character in text) - (1 if text else 0)


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
    occluders: set[tuple[int, int, int]] = set()
    for block in scan.get("blocks", []):
        if not isinstance(block, dict):
            continue
        raw_position = str(block.get("position", "")).split()
        if len(raw_position) < 3:
            continue
        try:
            parsed = tuple(float(value) for value in raw_position[:3])
            positions.append(parsed)  # type: ignore[arg-type]
            block_type = str(block.get("type", ""))
            if not any(token in block_type for token in ("glass", "air", "water")):
                occluders.add(tuple(int(value) for value in parsed))
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

    candidates: list[tuple[int, float, float, float]] = []
    camera_y = target[1] + max(5.0, extent * 0.7)
    for index in range(8):
        angle = math.radians(index * 45.0)
        camera_x = target[0] + math.cos(angle) * distance
        camera_z = target[2] + math.sin(angle) * distance
        score = 0
        for step in range(2, 17):
            ratio = step / 20.0
            point = (
                int(camera_x + (target[0] - camera_x) * ratio),
                int(camera_y + (target[1] - camera_y) * ratio),
                int(camera_z + (target[2] - camera_z) * ratio),
            )
            if point in occluders:
                score += 1
        candidates.append((score, index, camera_x, camera_z))
    candidates.sort(key=lambda item: (item[0], item[1]))
    first = candidates[0]
    opposite = [item for item in candidates[1:] if abs(int(item[1]) - int(first[1])) in {3, 4, 5}]
    second = min(opposite or candidates[1:], key=lambda item: (item[0], item[1]))
    views = [("front-right", first[2], first[3]), ("back-left", second[2], second[3])]
    result: list[tuple[str, tuple[str, float, float, float, float, float]]] = []
    for name, camera_x, camera_z in views:
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


from .runtime import workspace_root, player_id as validate_player_id


class GodBridge:
    def __init__(self, config: BridgeConfig, rcon: Any | None = None) -> None:
        self.config = config
        self.rcon = rcon or RconClient(config.rcon_host, config.rcon_port, config.rcon_password)
        self._lock = threading.RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=100)
        self._chat: deque[dict[str, Any]] = deque(maxlen=50)
        configured_state = self._absolute_path(config.state_file)
        self._owner_id = self._normalize_owner_id(config.god_player_id)
        if config.god_player_id and not self._owner_id:
            raise ValueError("god_player_id must be a valid UUID")
        if config.require_god_scope and not self._owner_id:
            raise ValueError("MINECRAFT_REQUIRE_GOD_SCOPE is enabled but no player UUID was provided")
        self._profile_dir = self._absolute_path(config.profile_dir)
        if self._owner_id:
            self._state_path = self._profile_dir / self._profile_key(self._owner_id) / "god_state.json"
        else:
            self._state_path = configured_state
        self._lock = ProfileLock(self._state_path.with_suffix(".lock"))
        self._events_path = self._absolute_path(config.events_file) if config.events_file else self._state_path.with_name("events.jsonl")
        ledger_path = self._absolute_path(config.ledger_file) if config.ledger_file else configured_state.with_name("god_ledger.sqlite3")
        self._ledger = GodLedger(ledger_path)
        self._next_event_id = 1
        self._god_state = self._load_state()
        self._refresh_events()
        self._inspection_turn = ""
        self._inspection_count = 0
        self._migration_warnings: list[dict[str, Any]] = []
        self._restore_landmark_ownership()
        self._restore_entity_ownership()

    def _restore_landmark_ownership(self) -> None:
        """Protect verified private landmarks created before the ownership ledger."""
        for profile in self._profile_dir.glob("*/god_state.json"):
            owner = self._normalize_owner_id(profile.parent.name)
            if not owner:
                continue
            with ProfileLock(profile.with_suffix(".lock")):
                state = self._load_state_from(profile)
            for key, value in state.get("landmarks", {}).items():
                if not isinstance(value, dict) or value.get("visibility", "private") != "private" or value.get("confidence", "verified") != "verified":
                    continue
                identifier = f"landmark:{owner}:{key}"
                if self._ledger.resource(identifier):
                    continue
                try:
                    x, y, z = absolute_block_position(value["position"])
                    self._ledger.claim_resource(identifier, "region", owner, {"dimension": normalize_dimension(value.get("dimension", "minecraft:overworld")), "participants": [owner], "bounds": value.get("bounds") or {"min": f"{x-4} {y-4} {z-4}", "max": f"{x+4} {y+4} {z+4}"}})
                except (ValueError, KeyError, PermissionError, TypeError) as error:
                    # Conflicting historical claims require an explicit agreement.
                    self._migration_warnings.append({"player_id": owner, "landmark_id": key, "error": str(error)})
                    continue

    def _restore_entity_ownership(self) -> None:
        identifier = "migration:entity-audit-v1"
        if self._ledger.resource(identifier):
            return
        if self._events_path.exists():
            with self._events_path.open(encoding="utf-8", errors="replace") as stream:
                for row in stream:
                    try:
                        event = json.loads(row)
                        owner = self._normalize_owner_id(event.get("player_uuid", ""))
                        command = str(event.get("command", ""))
                        if not owner or event.get("type") != "god_action" or event.get("source") != "god" or "summon " not in command or looks_like_command_failure(str(event.get("result", ""))):
                            continue
                        for tag_list in re.findall(r"Tags\s*:\s*\[([^\]]*)\]", command):
                            for tag in re.findall(r"['\"]([A-Za-z0-9_.-]{1,32})['\"]", tag_list):
                                self._ledger.claim_resource("entity:" + tag, "entity", owner, {"tag": tag, "legacy_event_id": event.get("event_id")})
                    except (ValueError, TypeError, PermissionError):
                        continue
        self._ledger.claim_resource(identifier, "migration", "system", {"completed_at": utc_now()})

    def _register_observed_entities(self, scan: dict[str, Any]) -> None:
        for entity in scan.get("entities", []):
            if not isinstance(entity, dict) or not entity.get("uuid"):
                continue
            if any((resource := self._ledger.resource("entity:" + tag)) and resource["owner_id"] == self._owner_id for tag in entity.get("tags", [])):
                self._ledger.claim_resource("entity:" + entity["uuid"], "entity", self._owner_id, {"tags": entity.get("tags", [])})

    @staticmethod
    def _normalize_owner_id(value: str) -> str:
        value = str(value or "").strip().lower()
        try:
            return str(uuid.UUID(value))
        except ValueError:
            return ""

    @staticmethod
    def _profile_key(player_id: str) -> str:
        return validate_player_id(player_id)

    def _absolute_path(self, value: str | Path | None) -> Path:
        if value is None:
            return Path("")
        path = Path(value)
        return path if path.is_absolute() else workspace_root() / path

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
        reference = str(reference or "").strip()
        profile = self._ledger.player(reference)
        if profile:
            profile = dict(profile)
            profile["resolved_reference"] = reference
            profile["match_method"] = "canonical_id_or_alias"
            return profile
        if reference and not re.fullmatch(r"[0-9a-f-]{16,}", reference.casefold()):
            candidates = self._ledger.players()
            scored = sorted(
                ((difflib.SequenceMatcher(None, reference.casefold(), str(item["name"]).casefold()).ratio(), item) for item in candidates),
                key=lambda item: item[0],
                reverse=True,
            )
            if scored and scored[0][0] >= 0.84 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08):
                profile = dict(scored[0][1])
                profile["resolved_reference"] = reference
                profile["match_method"] = "unambiguous_name_correction"
                return profile
        known = ", ".join(str(item["name"]) for item in self._ledger.players()) or "none registered"
        raise ValueError(f"unknown player profile: {reference}. Known players: {known}")

    @staticmethod
    def _event_dedupe_key(event: dict[str, Any]) -> str:
        if event.get("event_uuid"):
            return "paper:" + str(event["event_uuid"])
        explicit = event.get("dedupe_key")
        if explicit not in {None, ""}:
            return f"explicit:{explicit}"
        source = event.get("event_id")
        if source not in {None, ""}:
            return f"source:{event.get('type', 'unknown')}:{source}"
        return ""

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
        if not isinstance(state.get("memories"), list):
            state["memories"] = []
        if not isinstance(state.get("story_watches"), list):
            state["story_watches"] = []
        if not isinstance(state.get("story_objectives"), list):
            state["story_objectives"] = []
        if not isinstance(state.get("landmarks"), dict):
            state["landmarks"] = {}
        return state

    def _state_path_for(self, player_id: str) -> Path:
        if not player_id or (self._owner_id and player_id == self._owner_id):
            return self._state_path
        return self._profile_dir / self._profile_key(player_id) / "god_state.json"

    def _load_state_for(self, player_id: str) -> dict[str, Any]:
        return self._load_state_from(self._state_path_for(player_id))

    def _save_state(self) -> None:
        self._save_state_to(self._state_path, self._god_state)

    @staticmethod
    def _save_state_to(path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
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
        if event.get("type") == "voice_transcript":
            from .story import event_targets
            return event_targets(event, self._owner_id, self.current_player_name())
        fields = ("player_uuid", "victim_uuid", "killer_uuid", "target_player_id", "from_player_id", "to_player_id")
        if any(str(event.get(field, "")).lower() == self._owner_id for field in fields):
            return True
        owner_name = self.current_player_name().casefold()
        return any(str(event.get(field, "")).casefold() == owner_name for field in ("player", "victim", "killer", "target"))

    def record_event(self, event: dict[str, Any]) -> dict[str, Any]:
        clean = dict(event)
        clean.setdefault("type", "unknown")
        clean.setdefault("at", utc_now())
        clean.setdefault("source", "paper" if clean["type"] in {
            "chat", "player_chat", "player_prayer", "player_join", "player_quit",
            "player_death", "dimension_change", "advancement", "explosion_near_player",
            "player_snapshot", "block_place", "block_break", "mob_kill", "health_threshold",
        } else "external")
        dedupe_key = self._event_dedupe_key(clean)
        if dedupe_key:
            cached = self._ledger.cached_event(dedupe_key)
            if cached:
                return cached
        for id_key, name_key in (("player_uuid", "player"), ("victim_uuid", "victim"), ("killer_uuid", "killer")):
            player_id = str(clean.get(id_key, "") or "").strip().lower()
            player_name = str(clean.get(name_key, "") or "").strip()
            if player_id and player_name and clean["type"] != "voice_transcript":
                try:
                    self._ledger.register_player(player_id, player_name, online=clean["type"] != "player_quit")
                except ValueError:
                    pass
        automatic_consequence = False
        consequence_level = 0
        consequence_player_id = ""
        consequence_state = self._god_state
        consequence_path = self._state_path
        directed_abuse = bool(clean.get("private")) or bool(DIRECT_GOD_ADDRESS.search(str(clean.get("message", ""))))
        if clean.get("type") in {"chat", "player_chat", "player_prayer"} and directed_abuse and isinstance(clean.get("message"), str):
            player = str(clean.get("player", ""))
            player_id = self._event_player_id(clean)
            is_owner_event = bool(player_id and player_id == self._owner_id) or (not self._owner_id and player.casefold() == self.current_player_name().casefold())
            if player and (not self._owner_id or is_owner_event):
                conduct = assess_conduct(clean["message"])
                clean["conduct"] = conduct
                now = time.time()
                consequence_player_id = player_id or self._owner_id
                consequence_path = self._state_path_for(consequence_player_id) if consequence_player_id else self._state_path
                profile_lock = self._lock if consequence_path == self._state_path else ProfileLock(consequence_path.with_suffix(".lock"))
                with profile_lock:
                    consequence_state = self._load_state_from(consequence_path)
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
            clean.pop("dedupe_key", None)
            if supplied_event_id is not None:
                clean["source_event_id"] = supplied_event_id
            reserved, is_new = self._ledger.reserve_event(clean, dedupe_key=dedupe_key, minimum=self._next_event_id - 1)
            if not is_new:
                return reserved
            clean = reserved
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
        result = self._execute_targeted(f"godsmite {player}", player_id or self._owner_id, capability=True)
        result["action"] = "automatic_consequence"
        result["consequence_level"] = level
        state_path = self._state_path_for(player_id or self._owner_id)
        lock = self._lock if state_path == self._state_path else ProfileLock(state_path.with_suffix(".lock"))
        with lock:
            state = self._load_state_from(state_path)
            if result.get("ok"):
                state["last_major_intervention"] = f"Automatic consequence level {level}: lightning answered sustained blasphemy."
                self._save_state_to(state_path, state)
            if state_path == self._state_path:
                self._god_state = state
        return result

    def _refresh_events(self) -> None:
        # SQLite is authoritative and indexed. Do not rescan the growing audit
        # log every time a controller polls or a god asks for recent events.
        events = self._ledger.recent_events(100)
        if not events and not getattr(self, "_legacy_events_loaded", False):
            self._legacy_events_loaded = True
            if self._events_path.exists():
                with self._events_path.open("rb") as stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    stream.seek(max(0, size - 1024 * 1024))
                    if size > 1024 * 1024:
                        stream.readline()  # skip a partial first record
                    for row in stream.read().splitlines()[-100:]:
                        try:
                            event = json.loads(row)
                            if isinstance(event, dict):
                                events.append(event)
                        except (ValueError, UnicodeError):
                            continue
        if not events:
            return
        with self._lock:
            self._events.clear()
            self._chat.clear()
            for event in events:
                event_id = int(event.get("event_id", self._next_event_id))
                self._next_event_id = max(self._next_event_id, event_id + 1)
                self._events.append(event)
                if event.get("type") in {"chat", "player_chat"} or "message" in event:
                    self._chat.append(event)

    def _assert_command_authorized(self, command: str, *, target_player_id: str = "", capability: bool = False, allow_shared: bool = False) -> None:
        if not self._owner_id or capability or allow_shared:
            return
        authorize(self, command, target_player_id)

    def _execute_targeted(self, command: str, target_player_id: str, *, capability: bool = False) -> dict[str, Any]:
        self._assert_command_authorized(command, target_player_id=target_player_id, capability=capability)
        return self.execute(command, record=True, target_player_id=target_player_id, capability=capability)

    def execute(self, command: str, *, record: bool = True, target_player_id: str = "", capability: bool = False, allow_shared: bool = False, idempotency_key: str = "") -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        command_limit = 8192 if command.startswith("godcheckbatch ") else self.config.max_command_length
        if len(command) > command_limit:
            raise ValueError(f"command exceeds {self.config.max_command_length} characters")
        if any(char in command for char in "\r\n\x00"):
            raise ValueError("command cannot contain newlines or NUL bytes")
        command = command.strip().lstrip("/")
        context = self._turn_context()
        if (record or capability) and context.get("status") in {"interrupted", "expired"}:
            raise PermissionError("this decision was interrupted; wait for the new trigger before acting")
        if (record or capability) and context.get("deadline_epoch") and time.time() >= context["deadline_epoch"]:
            raise PermissionError("decision budget expired; preserve the work in state and use a bounded next step")
        self._assert_command_authorized(command, target_player_id=target_player_id, capability=capability, allow_shared=allow_shared)
        self._validate_command_preflight(command)
        if idempotency_key:
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
                raise ValueError("idempotency_key must contain only letters, numbers, dot, underscore, colon, or hyphen")
            cache_key = f"{self._owner_id}:{idempotency_key}" if self._owner_id else idempotency_key
            cached = self._ledger.cached_action(cache_key)
            if cached:
                replay = dict(cached)
                replay["idempotent_replay"] = True
                return replay
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
            if not output.strip() and re.search(r"\b(?:if|unless)\b", command):
                result["ok"] = False
            if command.split()[0].startswith("god"):
                try:
                    plugin_result = json.loads(output)
                    if isinstance(plugin_result, dict) and (plugin_result.get("ok") is False or plugin_result.get("error")):
                        result["ok"] = False
                        result["error"] = plugin_result.get("error", "Plugin rejected the action")
                except ValueError:
                    result["ok"] = False
            if not result["ok"]:
                result.setdefault("error", "Minecraft rejected the command or found no matching target.")
        if idempotency_key and result.get("ok"):
            self._ledger.remember_action(cache_key, result)
        if record:
            leaf = command.rsplit(" run ", 1)[-1]
            if result.get("ok") and re.match(r"(?:setblock|fill|summon|place|tp|teleport|give|clear|kill|effect|weather|time|godsmite|data (?:merge|modify|remove))\b", leaf):
                self._ledger.record_world_change(self._owner_id, {"command": command, "turn_id": context.get("turn_id"), "idempotency_key": idempotency_key, "confirmed": True})
            action_event: dict[str, Any] = {
                "type": "god_action",
                "source": "god",
                "wake_policy": "never",
                "command": command,
                "result": result.get("output", ""),
            }
            context = self._turn_context()
            if context.get("turn_id"):
                action_event["caused_by_turn_id"] = context["turn_id"]
            if self._owner_id:
                action_event.update({"player_uuid": self._owner_id, "player": self.current_player_name()})
            if target_player_id:
                action_event["target_player_id"] = target_player_id
            if idempotency_key:
                action_event["idempotency_key"] = idempotency_key
            self.record_event(action_event)
        return result

    def _validate_command_preflight(self, command: str) -> None:
        """Reject known-invalid command shapes before spending an RCON round trip."""
        lowered = command.casefold()
        if "front_text:" in lowered:
            color = re.search(r"(?:^|[,{}])color\s*:\s*['\"]?([a-z_]+)", lowered)
            if color and color.group(1) not in SIGN_COLORS:
                raise ValueError(f"unsupported sign color '{color.group(1)}'; use a vanilla dye color")
            messages = re.search(r"messages\s*:\s*\[([^\]]*)\]", command, re.IGNORECASE)
            if messages:
                lines = re.findall(r"'((?:\\'|[^'])*)'|\"((?:\\\"|[^\"])*)\"", messages.group(1))
                for index, pair in enumerate(lines, start=1):
                    line = next((part for part in pair if part != ""), "")
                    if len(line) > 15:
                        raise ValueError(f"sign line {index} exceeds 15 characters")
                    if _sign_line_width(line) > 90:
                        raise ValueError(f"sign line {index} exceeds the 90-pixel sign width")

    def preflight_command(self, command: str) -> dict[str, Any]:
        """Return the cheap, non-mutating checks used before raw commands."""
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if len(command) > self.config.max_command_length:
            raise ValueError(f"command exceeds {self.config.max_command_length} characters")
        if any(char in command for char in "\r\n\x00"):
            raise ValueError("command cannot contain newlines or NUL bytes")
        normalized = command.strip().lstrip("/")
        self._assert_command_authorized(normalized)
        self._validate_command_preflight(normalized)
        checks = ["single_line", "length_limit", "ownership_scope"]
        if "front_text:" in normalized.casefold():
            checks.append("sign_text_and_color")
        if re.search(r"\b(?:setblock|fill|summon|tp|place)\b", normalized.casefold()):
            checks.append("coordinate_command_shape")
        return {"ok": True, "command": normalized, "checks": checks, "mutating": True}

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
        radius = max(1, min(int(radius), 24))
        vertical_radius = max(1, min(int(vertical_radius), 16))
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
        self._register_observed_entities(scan)
        return {"ok": True, "scan": scan}

    def inspect_area_at(
        self,
        position: str,
        dimension: str = "minecraft:overworld",
        radius: int = 8,
        vertical_radius: int = 5,
    ) -> dict[str, Any]:
        """Inspect a remembered or chosen location without moving the player."""
        x, y, z = absolute_block_position(position)
        dimension = normalize_dimension(dimension)
        radius = max(1, min(int(radius), 24))
        vertical_radius = max(1, min(int(vertical_radius), 16))
        result = self.execute(
            f"godscanat {dimension} {x} {y} {z} {radius} {vertical_radius}",
            record=False,
            allow_shared=True,
        )
        if not result["ok"]:
            return {"ok": False, "error": result.get("output", "")}
        try:
            scan = json.loads(result.get("output", ""))
        except json.JSONDecodeError:
            return {"ok": False, "error": "godscanat returned non-JSON output", "output": result.get("output", "")}
        if isinstance(scan, dict) and scan.get("error"):
            return {"ok": False, "error": scan.get("error"), "scan": scan}
        self._register_observed_entities(scan)
        return {"ok": True, "scan": scan, "remote": True}

    def inspect_block(self, position: str, dimension: str = "") -> dict[str, Any]:
        """Inspect one exact block locally or in an explicitly named dimension."""
        turn_id = str(self._turn_context().get("turn_id", ""))
        if turn_id:
            if turn_id != self._inspection_turn:
                self._inspection_turn, self._inspection_count = turn_id, 0
            if self._inspection_count >= 12:
                raise ValueError("single-block inspection budget reached; use inspect_blocks, inspect_footprint, or find_safe_surface")
            self._inspection_count += 1
        if dimension:
            x, y, z = absolute_block_position(position)
            dimension = normalize_dimension(dimension)
            command = f"godcheckat {dimension} {x} {y} {z}"
        else:
            x, y, z = self.resolve_block_position(position)
            command = f"godcheck {self.current_player_name()} {x} {y} {z}"
        result = self.execute(command, record=False, allow_shared=bool(dimension))
        if not result["ok"]:
            return {"ok": False, "error": result.get("output", "godcheck failed"), "position": [x, y, z]}
        try:
            block = json.loads(result.get("output", ""))
        except json.JSONDecodeError:
            return {"ok": False, "error": "godcheck returned non-JSON output", "output": result.get("output", "")}
        return {"ok": True, "block": block}

    def inspect_blocks(self, positions: list[str], dimension: str = "") -> dict[str, Any]:
        """Batch exact block checks into one model-visible operation."""
        if not isinstance(positions, list) or not positions or len(positions) > 128:
            raise ValueError("positions must contain between 1 and 128 coordinate strings")
        base = self._player_position() if not dimension and any("~" in value for value in positions) else (0, 0, 0)
        resolved = [absolute_block_position(position) if dimension else tuple(_resolve_coordinate(token, coordinate, block=True) for token, coordinate in zip(parse_position(position).split(), base)) for position in positions]
        if not dimension:
            observed = self._compact_player_observation()
            if not observed.get("ok"):
                return observed
            dimension = observed["dimension"]
        command = "godcheckbatch " + normalize_dimension(dimension) + " " + " ".join(",".join(str(v) for v in point) for point in resolved)
        return self._plugin_json(command)

    def _plugin_json(self, command: str) -> dict[str, Any]:
        result = self.execute(command, record=False)
        try:
            value = json.loads(result["output"])
        except (ValueError, KeyError):
            return {"ok": False, "error": "compact observation unavailable; rebuild/install the Paper bridge plugin", "output": result.get("output", "")}
        if not isinstance(value, dict):
            return {"ok": False, "error": "invalid plugin observation"}
        return value

    def _compact_player_observation(self) -> dict[str, Any]:
        return self._plugin_json(f"godobserve {self.current_player_name()}")

    def find_safe_surface(self) -> dict[str, Any]:
        return self._plugin_json(f"godsurface {self.current_player_name()}")

    def inspect_footprint(self, start: str, end: str, dimension: str) -> dict[str, Any]:
        first, last = absolute_block_position(start), absolute_block_position(end)
        low = [min(a, b) for a, b in zip(first, last)]
        high = [max(a, b) for a, b in zip(first, last)]
        if math.prod(b - a + 1 for a, b in zip(low, high)) > 4096 or any(b - a > 47 for a, b in zip(low, high)):
            raise ValueError("footprint must fit 4096 blocks and 48 blocks per axis")
        return self._plugin_json(f"godfootprint {normalize_dimension(dimension)} " + " ".join(str(v) for v in low + high))

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
        return self._capture_scanned_scene(scene)

    def capture_scene_at(
        self,
        position: str,
        dimension: str = "minecraft:overworld",
        radius: int = 12,
        vertical_radius: int = 8,
    ) -> dict[str, Any]:
        """Capture a private remote landmark view without moving the player."""
        scene = self.inspect_area_at(position, dimension, radius, vertical_radius)
        return self._capture_scanned_scene(scene)

    def _capture_scanned_scene(self, scene: dict[str, Any]) -> dict[str, Any]:
        # SpectatorCamera has one global camera; hold a process-shared lock
        # across positioning, rendering, waiting, and copying both views.
        lock_path = self._absolute_path(self.config.camera_plugin_render_dir) / ".capture.lock"
        try:
            with ProfileLock(lock_path, timeout=30):
                return self._capture_scanned_scene_unlocked(scene)
        except TimeoutError:
            return {"ok": False, "error": "The camera is busy with another god. Retry later.", "scene": scene}

    def _capture_scanned_scene_unlocked(self, scene: dict[str, Any]) -> dict[str, Any]:
        if not scene.get("ok"):
            return {"ok": False, "error": "Could not capture because the live scene scan failed.", "scene": scene}

        workspace = workspace_root()
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
            destination = output_dir / f"scene-{uuid.uuid4().hex}-{view_name}.png"
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
        active_file = self._absolute_path(self.config.active_player_file)
        try:
            active_name = active_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        profile = self._ledger.player(active_name or self.config.player_name)
        return str(profile.get("player_id", "")) if profile else ""

    def current_player_name(self) -> str:
        if self._owner_id:
            return self._owner_profile()["name"]
        active_file = self._absolute_path(self.config.active_player_file)
        try:
            active_name = active_file.read_text(encoding="utf-8").strip()
        except OSError:
            active_name = ""
        if re.fullmatch(r"[A-Za-z0-9_]{1,16}", active_name):
            return active_name
        return self.config.player_name

    def teleport_preflight(self, position: str, dimension: str = "") -> dict[str, Any]:
        """Perform a quick destination check before moving the owned player."""
        x, y, z = absolute_block_position(position) if dimension else self.resolve_block_position(position)
        if self.config.dry_run:
            return {"ok": None, "status": "dry-run", "position": [x, y, z], "checks": []}
        checks = []
        for label, block_position in (
            ("feet", (x, y, z)),
            ("head", (x, y + 1, z)),
            ("support", (x, y - 1, z)),
        ):
            inspected = self.inspect_block("%d %d %d" % block_position, dimension)
            if not inspected.get("ok"):
                return {"ok": False, "status": "inspection-failed", "failed_check": label, "checks": checks, "error": inspected.get("error", "destination inspection failed")}
            block = inspected.get("block", {})
            checks.append({"name": label, "position": list(block_position), "block": block})
            if label in {"feet", "head"} and (block.get("liquid") or not block.get("passable", False)):
                return {"ok": False, "status": "occupied-or-liquid", "failed_check": label, "position": [x, y, z], "checks": checks}
            if label == "support" and (block.get("liquid") or not block.get("solid", False)):
                return {"ok": False, "status": "unsupported", "failed_check": label, "position": [x, y, z], "checks": checks}
        return {"ok": True, "status": "safe", "position": [x, y, z], "checks": checks}

    def register_player(self, player_id: str, name: str, *, online: bool = True) -> dict[str, Any]:
        return self._ledger.register_player(player_id, name, online=online)

    def player_profiles(self) -> list[dict[str, Any]]:
        return self._ledger.players()

    def observe_world(self, include_scene: bool = False) -> dict[str, Any]:
        player_name = self.current_player_name()
        observation: dict[str, Any] = {
            "observed_at": utc_now(),
            "player": player_name,
            "god_state": self.get_god_state(),
            "recent_events": self.recent_story_events(6),
            "recent_chat": [compact_event(event) for event in self.read_chat(4)],
            "agreements": self._ledger.agreements_for(self._owner_id) if self._owner_id else [],
            "recent_world_changes": self._ledger.world_changes_for(self._owner_id),
            "ownership_migration_warnings": self._migration_warnings[:12],
        }
        landmarks = self.list_landmarks()["landmarks"]
        trigger = json.dumps(self._turn_context().get("event", {})).casefold()
        observation["referenced_landmarks"] = [landmark for landmark in landmarks if any(
            re.search(r"(?<!\w)" + re.escape(str(name).casefold()) + r"(?!\w)", trigger)
            for name in [landmark.get("name", ""), landmark.get("id", "").replace("_", " ")] + landmark.get("aliases", []) if name
        )][:8]
        if self.config.dry_run:
            observation["connection"] = "dry_run"
            return observation

        observation["connection"] = "rcon"
        telemetry = self._compact_player_observation()
        observation["telemetry"] = telemetry
        observation["queries"] = {
            "position": "[" + ", ".join(f"{part}d" for part in str(telemetry.get("position", "")).split()) + "]",
            "health": telemetry.get("health"), "food": telemetry.get("food"), "dimension": telemetry.get("dimension", ""),
        }
        raw_position = str(observation["queries"].get("position", ""))
        coordinates = re.findall(r"(-?\d+(?:\.\d+)?)d", raw_position)
        current_dimension = ""
        dimension_match = re.search(r"minecraft:[a-z0-9_./-]+", str(observation["queries"].get("dimension", "")), re.IGNORECASE)
        if dimension_match:
            current_dimension = dimension_match.group(0).casefold()
        nearby = []
        if len(coordinates) >= 3:
            current = tuple(float(value) for value in coordinates[-3:])
            for landmark in landmarks:
                if not isinstance(landmark, dict) or str(landmark.get("dimension", "")).casefold() != current_dimension:
                    continue
                try:
                    target = tuple(float(value) for value in str(landmark.get("position", "")).split())
                except ValueError:
                    continue
                if len(target) != 3:
                    continue
                distance = round(math.dist(current, target), 1)
                if distance <= 256:
                    nearby.append({"id": landmark.get("id"), "name": landmark.get("name"), "distance": distance, "position": landmark.get("position")})
        observation["nearby_landmarks"] = sorted(nearby, key=lambda item: item["distance"])[:8]
        if include_scene:
            try:
                observation["scene"] = self.inspect_area()
            except Exception as exc:
                observation["scene"] = {"ok": False, "error": str(exc)}
        else:
            observation["scene"] = {"status": "not_requested", "hint": "Use inspect_area only when exact local geometry matters."}
        return observation

    def recent_story_events(self, limit: int = 10) -> list[dict[str, Any]]:
        audit_only = {"god_action", "player_snapshot", "block_place", "block_break", "mob_kill", "health_threshold"}
        return [compact_event(event) for event in self.recent_events(50) if event.get("type") not in audit_only][-max(1, min(int(limit), 20)):]

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
        durable = self._ledger.events_since(after_event_id, limit)
        if durable:
            return [event for event in durable if self.event_matches_owner(event)]
        self._refresh_events()
        with self._lock:
            return [event for event in self._events if int(event.get("event_id", 0)) > after_event_id and self.event_matches_owner(event)][:max(1, min(limit, 200))]

    def latest_event_id(self) -> int:
        self._refresh_events()
        with self._lock:
            return max(self._next_event_id - 1, self._ledger.latest_event_id())

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
            scalar_updates = dict(updates)
            incoming_memories = scalar_updates.pop("memories", None)
            incoming_landmarks = scalar_updates.pop("landmarks", None)
            self._god_state.update(scalar_updates)
            if incoming_memories is not None:
                if not isinstance(incoming_memories, list):
                    raise ValueError("memories must be an array")
                memories = [str(item).strip() for item in self._god_state.get("memories", []) if str(item).strip()]
                for item in incoming_memories:
                    memory = str(item).strip()
                    if memory and memory not in memories:
                        memories.append(memory)
                self._god_state["memories"] = memories[-MAX_MEMORIES:]
            if incoming_landmarks is not None:
                raise ValueError("use remember_landmark so dimensions, coordinates and ownership are validated")
            self._save_state()
            return self.get_god_state()

    @profile_transaction
    def remember_landmark(self, landmark: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(landmark, dict):
            raise ValueError("landmark must be an object")
        name = str(landmark.get("name", "")).strip()
        key = landmark_key(str(landmark.get("id") or name))
        position = str(landmark.get("position", "")).strip()
        x, y, z = absolute_block_position(position)
        dimension = normalize_dimension(str(landmark.get("dimension", "minecraft:overworld")))
        aliases = landmark.get("aliases", [])
        if not isinstance(aliases, list):
            raise ValueError("landmark aliases must be an array")
        clean: dict[str, Any] = {
            "id": key,
            "name": name or key.replace("_", " ").title(),
            "dimension": dimension,
            "position": f"{x} {y} {z}",
            "aliases": list(dict.fromkeys(str(item).strip()[:64] for item in aliases if str(item).strip()))[:12],
            "notes": str(landmark.get("notes", ""))[:1000],
            "confidence": str(landmark.get("confidence", "verified"))[:32],
            "visibility": str(landmark.get("visibility", "private"))[:32],
            "last_verified_at": str(landmark.get("last_verified_at") or utc_now()),
        }
        bounds = landmark.get("bounds")
        if bounds is not None and bounds != "":
            if not isinstance(bounds, dict) or not {"min", "max"} <= set(bounds):
                raise ValueError("landmark bounds must contain min and max coordinate strings")
            min_pos = absolute_block_position(str(bounds["min"]))
            max_pos = absolute_block_position(str(bounds["max"]))
            clean["bounds"] = {
                "min": " ".join(str(min(a, b)) for a, b in zip(min_pos, max_pos)),
                "max": " ".join(str(max(a, b)) for a, b in zip(min_pos, max_pos)),
            }
        if self._owner_id and clean["visibility"] == "private" and clean["confidence"] == "verified":
            protected_bounds = clean.get("bounds", {"min": f"{x - 4} {y - 4} {z - 4}", "max": f"{x + 4} {y + 4} {z + 4}"})
            self._ledger.claim_resource(f"landmark:{self._owner_id}:{key}", "region", self._owner_id, {"dimension": dimension, "bounds": protected_bounds, "participants": [self._owner_id]})
        with self._lock:
            self._god_state = self._load_state_from(self._state_path)
            landmarks = dict(self._god_state.get("landmarks", {}))
            existing = landmarks.get(key, {}) if isinstance(landmarks.get(key), dict) else {}
            existing.update(clean)
            landmarks[key] = existing
            self._god_state["landmarks"] = dict(list(landmarks.items())[-MAX_LANDMARKS:])
            self._save_state()
        return {"ok": True, "landmark": existing, "landmark_count": len(self._god_state["landmarks"])}

    def list_landmarks(self, query: str = "") -> dict[str, Any]:
        state = self.get_god_state()
        landmarks = list(state.get("landmarks", {}).values())
        if self._owner_id:
            shared = [dict(value["payload"]["terms"], agreement_id=value["id"], visibility="shared", confidence="agreed") for value in self._ledger.agreements_for(self._owner_id, "active") if value["kind"] == "shared_landmark"]
            shared_ids = {value["id"] for value in shared}
            landmarks = [value for value in landmarks if value.get("id") not in shared_ids] + shared
        query = str(query or "").strip().casefold()
        if query:
            landmarks = [item for item in landmarks if query in " ".join([
                str(item.get("id", "")), str(item.get("name", "")),
                " ".join(str(alias) for alias in item.get("aliases", [])),
                str(item.get("notes", "")),
            ]).casefold()]
        return {"ok": True, "landmarks": landmarks, "count": len(landmarks)}

    def forget_landmark(self, landmark_id: str) -> dict[str, Any]:
        key = landmark_key(landmark_id)
        with self._lock:
            self._god_state = self._load_state_from(self._state_path)
            landmarks = dict(self._god_state.get("landmarks", {}))
            removed = landmarks.pop(key, None)
            self._god_state["landmarks"] = landmarks
            self._save_state()
        if self._owner_id:
            self._ledger.release_resource(f"landmark:{self._owner_id}:{key}", self._owner_id)
        return {"ok": True, "removed": bool(removed), "landmark_id": key}

    def propose_agreement(self, subject: str, participants: list[str], terms: dict[str, Any], kind: str = "treaty", supersedes: str = "") -> dict[str, Any]:
        owner = self._require_owner()
        identities = [self._resolve_player(reference)["player_id"] for reference in participants]
        if not isinstance(terms, dict) or not terms:
            raise ValueError("agreement terms must be a nonempty object")
        terms = dict(terms)
        if kind == "shared_landmark":
            terms["id"] = landmark_key(str(terms.get("id") or subject))
            terms["name"] = str(terms.get("name") or subject)
            terms["dimension"] = normalize_dimension(str(terms.get("dimension", "minecraft:overworld")))
            terms["position"] = " ".join(str(v) for v in absolute_block_position(str(terms.get("position", ""))))
            bounds = terms.get("bounds", {})
            if not isinstance(bounds, dict) or not {"min", "max"} <= bounds.keys():
                raise ValueError("shared landmarks require explicit protected bounds")
            low, high = [absolute_block_position(str(bounds[key])) for key in ["min", "max"]]
            terms["bounds"] = {"min": " ".join(str(min(a,b)) for a,b in zip(low,high)), "max": " ".join(str(max(a,b)) for a,b in zip(low,high))}
            terms.setdefault("aliases", [])
            terms.setdefault("notes", "")
        value = self._ledger.propose_agreement(owner, subject, kind, identities, terms, supersedes)
        self._agreement_event("agreement_proposal", value)
        return {"ok": True, "agreement": value}

    def respond_agreement(self, agreement_id: str, decision: str) -> dict[str, Any]:
        value = self._ledger.respond_agreement(agreement_id, self._require_owner(), decision)
        self._agreement_event("agreement_response", value)
        return {"ok": True, "agreement": value}

    def _agreement_event(self, kind: str, value: dict[str, Any]) -> None:
        self.record_event({"type": kind, "source": "god", "wake_policy": "always", "agreement_id": value["id"], "from_player_id": self._owner_id, "participant_ids": value["payload"]["participants"], "status": value["status"], "subject": value["subject"]})

    @profile_transaction
    def record_player_choice(self, choice: str, evidence: str, objective_id: str = "", approach: str = "preparation") -> dict[str, Any]:
        if approach not in {"progress", "preparation", "incidental", "refusal", "abandonment", "alternative"}:
            raise ValueError("unsupported choice interpretation")
        if not choice.strip() or not evidence.strip():
            raise ValueError("a player choice needs concrete evidence")
        state = self.get_god_state()
        record = {"choice": choice[:500], "evidence": evidence[:1000], "approach": approach, "objective_id": objective_id, "at": utc_now()}
        history = state.get("player_choice_history", []) + [record]
        updates: dict[str, Any] = {"player_choice": choice[:500], "player_choice_history": history[-24:]}
        if objective_id:
            objectives = state.get("story_objectives", [])
            objective = next((item for item in objectives if item.get("id") == objective_id), None)
            if not objective:
                raise ValueError("unknown objective")
            objective["last_choice"] = record
            updates["story_objectives"] = objectives
        self.update_god_state(updates)
        return {"ok": True, "record": record}

    def reserve_entity_tag(self, requested: str = "", idempotency_key: str = "") -> str:
        owner = self._require_owner()
        suffix = uuid.uuid5(uuid.NAMESPACE_OID, owner + ":" + idempotency_key).hex[:20] if idempotency_key else uuid.uuid4().hex[:20]
        tag = requested or "god_" + suffix
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", tag):
            raise ValueError("invalid entity ownership tag")
        existing = self._ledger.resource("entity:" + tag)
        if existing and existing["owner_id"] == owner and (not idempotency_key or existing["payload"].get("idempotency_key") != idempotency_key):
            raise ValueError("entity tag is already reserved; move the existing entity or choose a new tag")
        self._ledger.claim_resource("entity:" + tag, "entity", owner, {"tag": tag, "idempotency_key": idempotency_key})
        return tag

    @profile_transaction
    def revise_story_objective(self, objective_id: str, reason: str, objective: dict[str, Any]) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("objective revisions require the player's reason or choice")
        state = self.get_god_state()
        previous = next((value for value in state["story_objectives"] if value["id"] == objective_id), None)
        if not previous or previous["status"] not in {"active", "achieved_pending_judgment"}:
            raise ValueError("only unresolved objectives may be revised")
        replacement = dict(objective, id=objective_id)
        self.record_player_choice(reason, "Negotiated objective revision: " + previous["description"], objective_id, "alternative")
        result = self.set_story_objective(replacement)
        if previous.get("watch_id") != result["objective"]["watch_id"]:
            self.clear_story_watch(previous["watch_id"])
        refreshed = self.get_god_state()
        refreshed["unresolved_promises"] = [promise for promise in refreshed["unresolved_promises"] if promise != previous["description"] or promise == result["objective"]["description"]]
        new_objective = next(item for item in refreshed["story_objectives"] if item["id"] == objective_id)
        new_objective["revision_reason"] = reason[:1000]
        new_objective["previous_description"] = previous["description"]
        self.update_god_state({"unresolved_promises": refreshed["unresolved_promises"], "story_objectives": refreshed["story_objectives"]})
        return result

    def observe_location(
        self,
        landmark_id: str,
        radius: int = 12,
        vertical_radius: int = 8,
        include_scene: bool = True,
    ) -> dict[str, Any]:
        matches = self.list_landmarks(landmark_id)["landmarks"]
        exact = [item for item in matches if item.get("id") == landmark_key(landmark_id)]
        if exact:
            landmark = exact[0]
        elif len(matches) == 1:
            landmark = matches[0]
        else:
            raise ValueError(f"landmark '{landmark_id}' is unknown or ambiguous")
        result: dict[str, Any] = {"ok": True, "landmark": landmark, "observed_at": utc_now()}
        if include_scene:
            scene = self.inspect_area_at(landmark["position"], landmark["dimension"], radius, vertical_radius)
            result["ok"] = bool(scene.get("ok"))
            result["scene"] = scene
        return result

    @profile_transaction
    def set_story_objective(self, objective: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(objective, dict):
            raise ValueError("objective must be an object")
        description = str(objective.get("description", "")).strip()
        watch = objective.get("watch")
        if not description or not isinstance(watch, dict):
            raise ValueError("objective requires a description and deterministic watch")
        objective_id = landmark_key(str(objective.get("id") or f"objective_{int(time.time())}"))
        watch = dict(watch)
        alternatives = objective.get("alternatives", [])
        if not isinstance(alternatives, list) or len(alternatives) > 6:
            raise ValueError("objective alternatives must be a list of at most six described watches")
        if alternatives:
            if any(not isinstance(item, dict) or not isinstance(item.get("watch"), dict) or not item.get("description") for item in alternatives):
                raise ValueError("each alternative requires a description and a deterministic watch")
            watch = {"kind": "any", "conditions": [watch] + [item["watch"] for item in alternatives]}
        watch.setdefault("description", description)
        watch["objective_id"] = objective_id
        registered = self.register_story_watch(watch)
        clean = {
            "id": objective_id,
            "description": description,
            "status": "active",
            "created_at": utc_now(),
            "watch_id": registered["watch"]["id"],
            "landmark_id": str(objective.get("landmark_id", ""))[:64],
            "completion_note": str(objective.get("completion_note", ""))[:500],
            "purpose": str(objective.get("purpose", description))[:500],
            "alternatives": objective.get("alternatives", [])[:8],
            "failure_policy": str(objective.get("failure_policy", "Judge deliberate choices; preparation and incidental activity are not failure."))[:500],
        }
        with self._lock:
            self._god_state = self._load_state_from(self._state_path)
            objectives = [item for item in self._god_state.get("story_objectives", []) if isinstance(item, dict) and item.get("id") != objective_id]
            objectives.append(clean)
            self._god_state["story_objectives"] = objectives[-MAX_OBJECTIVES:]
            promises = [str(item) for item in self._god_state.get("unresolved_promises", []) if str(item)]
            if description not in promises:
                promises.append(description)
            self._god_state["unresolved_promises"] = promises[-MAX_OBJECTIVES:]
            self._save_state()
        return {"ok": True, "objective": clean, "watch": registered["watch"]}

    @profile_transaction
    def complete_story_objective(self, objective_id: str, outcome: str = "fulfilled") -> dict[str, Any]:
        key = landmark_key(objective_id)
        outcome = str(outcome or "fulfilled").strip().casefold()
        if outcome not in {"fulfilled", "failed", "abandoned"}:
            raise ValueError("outcome must be fulfilled, failed, or abandoned")
        with self._lock:
            self._god_state = self._load_state_from(self._state_path)
            objective = next((item for item in self._god_state.get("story_objectives", []) if isinstance(item, dict) and item.get("id") == key), None)
            if not objective:
                raise ValueError(f"unknown story objective: {objective_id}")
            objective["status"] = outcome
            objective["resolved_at"] = utc_now()
            description = str(objective.get("description", ""))
            self._god_state["unresolved_promises"] = [item for item in self._god_state.get("unresolved_promises", []) if item != description]
            watch_id = str(objective.get("watch_id", ""))
            self._god_state["story_watches"] = [item for item in self._god_state.get("story_watches", []) if not isinstance(item, dict) or item.get("id") != watch_id]
            self._save_state()
        return {"ok": True, "objective": objective}

    def _turn_context(self) -> dict[str, Any]:
        path = self._absolute_path(self.config.turn_context_file)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _speech_guard(self, message: str) -> tuple[dict[str, Any], str]:
        context = self._turn_context()
        turn_id = str(context.get("turn_id", ""))
        state = self.get_god_state()
        if turn_id and state.get("last_spoken_turn_id") == turn_id:
            raise PermissionError("only one player-visible message is allowed in a God turn")
        previous = state.get("last_player_visible_message")
        if isinstance(previous, dict):
            old_text = re.sub(r"\s+", " ", str(previous.get("text", "")).casefold()).strip()
            new_text = re.sub(r"\s+", " ", message.casefold()).strip()
            sent_at = _parse_epoch(previous.get("at_epoch"))
            if old_text and old_text == new_text and sent_at is not None and time.time() - sent_at < self.config.repeated_speech_cooldown:
                raise PermissionError("a substantially identical player-visible message was sent recently")
        return state, turn_id

    def _remember_speech(self, state: dict[str, Any], turn_id: str, message: str) -> None:
        state["last_player_visible_message"] = {"text": message.strip(), "at": utc_now(), "at_epoch": _epoch_string(time.time())}
        if turn_id:
            state["last_spoken_turn_id"] = turn_id
        with self._lock:
            self._god_state = self._load_state_from(self._state_path)
            self._god_state.update({
                "last_player_visible_message": state["last_player_visible_message"],
                "last_spoken_turn_id": state.get("last_spoken_turn_id", ""),
            })
            self._save_state()

    def register_story_watch(self, watch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(watch, dict):
            raise ValueError("watch must be an object")
        kind = str(watch.get("kind", "event")).casefold()
        if kind not in WATCH_KINDS:
            raise ValueError(f"watch kind must be one of: {', '.join(sorted(WATCH_KINDS))}")
        clean = validate_watch(watch)
        def normalize_watch_dimension(value):
            if value.get("dimension"):
                value["dimension"] = normalize_dimension(value["dimension"])
            for condition in value.get("conditions", []):
                normalize_watch_dimension(condition)
        normalize_watch_dimension(clean)
        clean["kind"] = kind
        clean["id"] = str(clean.get("id") or f"watch-{int(time.time())}-{len(self.get_god_state().get('story_watches', [])) + 1}")[:128]
        clean["created_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        if not clean.get("description"):
            raise ValueError("watch description is required")
        with self._lock:
            self._god_state = self._load_state_from(self._state_path)
            watches = [item for item in self._god_state.get("story_watches", []) if isinstance(item, dict) and item.get("id") != clean["id"]]
            watches.append(clean)
            active_ids = {item["id"] for item in self._god_state.get("story_objectives", []) if isinstance(item, dict) and item.get("status") in {"active", "achieved_pending_judgment"}}
            while len(watches) > 20:
                removable = next((item for item in watches if item.get("objective_id") not in active_ids and item["id"] != clean["id"]), None)
                if removable is None:
                    raise ValueError("resolve an existing objective before registering another watch")
                watches.remove(removable)
            self._god_state["story_watches"] = watches
            self._save_state()
        return {"ok": True, "watch": clean, "watch_count": len(watches[-20:])}

    def clear_story_watch(self, watch_id: str) -> dict[str, Any]:
        watch_id = str(watch_id or "").strip()
        if not watch_id:
            raise ValueError("watch_id is required")
        with self._lock:
            self._god_state = self._load_state_from(self._state_path)
            previous = self._god_state.get("story_watches", [])
            watches = [item for item in previous if not isinstance(item, dict) or item.get("id") != watch_id]
            self._god_state["story_watches"] = watches
            self._save_state()
        return {"ok": True, "watch_id": watch_id, "removed": len(previous) - len(watches)}

    def god_say(self, message: str) -> dict[str, Any]:
        """Backward-compatible private speech to this god's own player."""
        return self.god_whisper(message)

    def god_broadcast(self, message: str) -> dict[str, Any]:
        """Make an explicitly public, attributed pantheon announcement."""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if len(message) > 256:
            raise ValueError("god message cannot exceed 256 characters")
        state, turn_id = self._speech_guard(message)
        speaker = f"[God of {self.current_player_name()}] "
        component = [
            {"text": speaker, "color": "dark_purple", "bold": True},
            {"text": message, "color": "light_purple"},
        ]
        tellraw = f"tellraw @a {json.dumps(component, separators=(',', ':'))}"
        result = self.execute(tellraw, allow_shared=True)
        if result.get("ok"):
            self._remember_speech(state, turn_id, message)
        result.update({"message": message, "speaker": speaker, "public": True})
        return result

    def god_whisper(self, message: str) -> dict[str, Any]:
        owner_id = self._owner_id
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if len(message) > 256:
            raise ValueError("god message cannot exceed 256 characters")
        state, turn_id = self._speech_guard(message)
        player = self.current_player_name()
        tellraw = f"tellraw {player} {{\"text\":{json.dumps(message)},\"color\":\"dark_purple\",\"bold\":true}}"
        result = self._execute_targeted(tellraw, owner_id)
        if result.get("ok"):
            self._remember_speech(state, turn_id, message)
        result.update({"message": message, "player": player, "private": True})
        return result

    def send_god_message(
        self,
        target: str,
        message: str,
        *,
        subject: str = "",
        event_id: int | None = None,
        message_type: str = "intent",
        verified: bool = False,
    ) -> dict[str, Any]:
        owner_id = self._require_owner()
        target_profile = self._resolve_player(target)
        if target_profile["player_id"] == owner_id:
            raise ValueError("use whisper_player to speak to your own player")
        if not isinstance(message, str) or not message.strip() or len(message) > 1000:
            raise ValueError("message must be between 1 and 1000 characters")
        message_type = str(message_type or "intent").strip().casefold()
        if message_type not in MESSAGE_TYPES:
            raise ValueError(f"message_type must be one of: {', '.join(sorted(MESSAGE_TYPES))}")
        if message_type == "verified_fact" and not verified:
            raise ValueError("verified_fact messages must set verified=true")
        interaction = self._ledger.add_interaction(
            kind="god_message",
            from_player_id=owner_id,
            to_player_id=target_profile["player_id"],
            status="delivered",
            message=message.strip(),
            reason=subject.strip()[:256],
            event_id=event_id,
            payload={
                "message_type": message_type,
                "verified": bool(verified),
                "resolved_reference": target_profile.get("resolved_reference", target),
            },
        )
        self.record_event({
            "type": "god_message",
            "source": "god",
            "wake_policy": "recipient",
            "from_player_id": owner_id,
            "from_player": self.current_player_name(),
            "to_player_id": target_profile["player_id"],
            "to_player": target_profile["name"],
            "message": message.strip(),
            "subject": subject.strip()[:256],
            "message_type": message_type,
            "verified": bool(verified),
            "interaction_id": interaction["id"],
            "source_event_id": event_id,
        })
        return {
            "ok": True,
            "delivery": {
                "status": "delivered",
                "target_player_id": target_profile["player_id"],
                "target_player": target_profile["name"],
                "matched_reference": target_profile.get("resolved_reference", target),
                "match_method": target_profile.get("match_method", "canonical_id_or_alias"),
            },
            "interaction": interaction,
        }

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
            "source": "god",
            "wake_policy": "recipient",
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
            "source": "god",
            "wake_policy": "requester",
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
        command = f"godsmite {target['name']}"
        result = self._execute_targeted(command, target["player_id"], capability=True)
        if not self.config.dry_run:
            try:
                result["ok"] = bool(json.loads(result["output"]).get("ok"))
            except ValueError:
                result["ok"] = False
        result.update({"action": "approved_smite", "request_id": request_id, "target": target["name"]})
        self.record_event({
            "type": "intervention_used",
            "source": "god",
            "wake_policy": "never",
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
            f"godsmite {player}"
            if mode == "smite" else "weather thunder 120"
        )
        result = self.execute(command, capability=mode == "smite")
        result["action"] = "punish_player"
        if result.get("ok"):
            with self._lock:
                self._god_state = self._load_state_from(self._state_path)
                self._god_state["last_major_intervention"] = f"Delivered deliberate {mode} consequence to {player}."
                self._save_state()
        return result

    def action(self, name: str, command_builder: Callable[[], str], *, idempotency_key: str = "") -> dict[str, Any]:
        command = command_builder()
        result = self.execute(command, idempotency_key=idempotency_key)
        result["action"] = name
        return result

    def structure_library(self) -> StructureLibrary:
        return StructureLibrary(
            workspace_root(),
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
        enable = self.execute(f'datapack enable "file/{result["pack_name"]}"', record=False, capability=True)
        reload_result = self.execute("reload", record=False, capability=True) if enable["ok"] else {"ok": False, "error": "datapack was not enabled"}
        result["activation"] = {"enable": enable, "reload": reload_result}
        result["ok"] = bool(result.get("ok") and enable.get("ok") and reload_result.get("ok"))
        return result

    def place_structure(
        self,
        structure: str,
        position: str,
        rotation: str = "none",
        preview: bool = False,
        idempotency_key: str = "",
        dimension: str = "",
    ) -> dict[str, Any]:
        if rotation not in {"none", "clockwise_90", "clockwise_180", "counterclockwise_90"}:
            raise ValueError("invalid structure rotation")
        entry = self.structure_library().get(structure)
        if dimension:
            dimension = normalize_dimension(dimension)
            block_position = absolute_block_position(position)
        elif self.config.dry_run and "~" in position:
            block_position = (0, 0, 0)
        else:
            block_position = self.resolve_block_position(position)
        size = entry.get("size", []) if entry else []
        footprint_origin, footprint_size = block_position, size
        if len(size) == 3:
            if any(int(value) < 1 for value in size):
                raise ValueError("structure dimensions must be positive")
            corners = [_rotate_structure_position([x, y, z], size, rotation) for x in [0, int(size[0])-1] for y in [0, int(size[1])-1] for z in [0, int(size[2])-1]]
            minimum = [min(corner[i] for corner in corners) for i in range(3)]
            maximum = [max(corner[i] for corner in corners) for i in range(3)]
            footprint_origin = tuple(block_position[i] + minimum[i] for i in range(3))
            footprint_size = [maximum[i] - minimum[i] + 1 for i in range(3)]
        if self._owner_id:
            if len(size) < 3:
                raise PermissionError("scoped structure placement requires imported size metadata to check its full protected footprint")
            if not entry.get("scoped_safe"):
                raise PermissionError("reimport a template without command-bearing blocks or embedded entities; summon owned entities separately")
            start = [str(v) for v in footprint_origin]
            end = [str(footprint_origin[i] + footprint_size[i] - 1) for i in range(3)]
            check_world_bounds(self, start, end, dimension, not bool(dimension))
        preflight = self._structure_preflight(footprint_origin, footprint_size, dimension) if size and not self.config.dry_run else {"ok": None, "status": "not-run"}
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
        command_rotation = "180" if rotation == "clockwise_180" else rotation
        command = f"place template {structure} {command_position} {command_rotation}"
        if dimension:
            command = f"execute in {dimension} run {command}"
        else:
            command = self.command_at_player(command)
        if preview:
            return {
                "ok": True,
                "action": "place_structure_preview",
                "preview": True,
                "structure": structure,
                "origin": None if self.config.dry_run and "~" in position else {"x": block_position[0], "y": block_position[1], "z": block_position[2]},
                "rotation": rotation,
                "command": command,
                "preflight": preflight,
                "verification": {"ok": None, "status": "not-mutated"},
            }
        result = self.action("place_structure", lambda: command, idempotency_key=idempotency_key)
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
            samples.append({"position": target, "expected": block_name})
        if samples:
            batch = self.inspect_blocks([" ".join(str(value) for value in sample["position"]) for sample in samples], dimension)
            checks = batch.get("results", [])
            for index, sample in enumerate(samples):
                check = checks[index] if index < len(checks) else {"ok": False}
                sample["result"] = check
                sample["matched"] = check.get("ok") and check.get("block", {}).get("type") == sample["expected"]
        result["size"] = size
        result["verification"] = {"ok": bool(samples) and all(item["matched"] for item in samples), "samples": samples}
        result["ok"] = bool(result["ok"] and result["verification"]["ok"])
        return result

    def _structure_preflight(self, origin: tuple[int, int, int], size: list[Any], dimension: str = "") -> dict[str, Any]:
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
        ordered = sorted(points)
        batch = self.inspect_blocks([" ".join(str(v) for v in point) for point in ordered], dimension)
        results = batch.get("results", [])
        for index, point in enumerate(ordered):
            inspected = results[index] if index < len(results) else {"ok": False, "error": batch.get("error", "missing batch result")}
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
            "not loaded",
            "outside of the world",
            "no blocks were filled",
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
    if rotation == "clockwise_90":
        return -z, y, x
    if rotation == "clockwise_180":
        return -x, y, -z
    if rotation == "counterclockwise_90":
        return z, y, -x
    return x, y, z


def parse_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:.\-/]+", value):
        raise ValueError(f"{field} must be a Minecraft identifier")
    return value
