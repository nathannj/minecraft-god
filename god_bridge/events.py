"""Validate the versioned, untrusted Paper-to-bridge event contract."""
from __future__ import annotations

import json
import math
import re
from typing import Any

from .runtime import player_id


MAX_EVENT_BYTES = 64 * 1024
PLAYER_EVENTS = {
    "chat", "player_chat", "player_prayer", "player_join", "player_quit",
    "player_death", "dimension_change", "advancement", "explosion_near_player",
    "player_snapshot", "block_place", "block_break", "item_acquired", "mob_kill",
    "health_threshold", "voice_transcript", "map_waypoint",
}
SPATIAL_EVENTS = {"player_snapshot", "block_place", "block_break", "mob_kill", "explosion_near_player", "voice_transcript", "map_waypoint"}


def validate_event(value: Any) -> dict[str, Any]:
    """Validate external input before it reaches state, routing, or paths."""
    if not isinstance(value, dict):
        raise ValueError("event payload must be an object")
    event = dict(value)
    kind = event.get("type")
    if not isinstance(kind, str) or kind not in PLAYER_EVENTS:
        raise ValueError("unsupported Paper event type")
    if type(event.get("schema_version", 1)) is not int or event.get("schema_version", 1) != 1:
        raise ValueError("unsupported event schema version")
    try:
        if len(json.dumps(event, allow_nan=False).encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("event body is too large")
    except (TypeError, OverflowError) as error:
        raise ValueError("event must contain finite JSON values") from error
    for key in ("player_uuid", "victim_uuid", "killer_uuid", "from_player_id", "to_player_id", "target_player_id"):
        if key in event:
            event[key] = player_id(event[key], canonical_uuid=True)
    for key in ("player", "victim", "killer"):
        if key in event and (not isinstance(event[key], str) or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", event[key])):
            raise ValueError(f"{key} must be a Minecraft Java player name")
    if kind in PLAYER_EVENTS and not all(event.get(key) for key in ("player_uuid", "player")):
        raise ValueError(f"{kind} requires player and player_uuid")
    if kind in SPATIAL_EVENTS:
        dimension = event.get("dimension")
        if not isinstance(dimension, str) or not re.fullmatch(r"[a-z0-9_.-]+:[a-z0-9_./-]+", dimension):
            raise ValueError(f"{kind} requires a namespaced dimension")
        try:
            coordinates = [float(part) for part in event.get("position", "").split()]
            if len(coordinates) != 3 or not all(math.isfinite(v) and abs(v) <= 30_000_000 for v in coordinates):
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError(f"{kind} requires three finite world coordinates") from error
    if "message" in event and (not isinstance(event["message"], str) or len(event["message"]) > 4096):
        raise ValueError("event message must be at most 4096 characters")
    if kind in {"block_place", "block_break"} and not isinstance(event.get("block"), str):
        raise ValueError("block events require a block identifier")
    if kind == "player_snapshot" and not isinstance(event.get("inventory"), dict):
        raise ValueError("snapshots require an inventory object")
    if kind == "player_snapshot" and any(not isinstance(key, str) or type(amount) is not int or amount < 0 for key, amount in event["inventory"].items()):
        raise ValueError("inventory quantities must be nonnegative integers")
    if "health" in event and (isinstance(event["health"], bool) or not isinstance(event["health"], (int, float)) or not math.isfinite(event["health"])):
        raise ValueError("health must be a finite number")
    event["schema_version"] = 1
    event["source"] = "paper"
    event.pop("event_id", None)
    if "event_uuid" in event:
        event["event_uuid"] = player_id(event["event_uuid"], canonical_uuid=True)
    if kind == "voice_transcript":
        if event.get("voice_mode") not in {"proximity", "whisper"}:
            raise ValueError("only proximity and whisper speech is accepted")
        for key in ("nearby_players", "routed_listeners"):
            people = event.get(key)
            if not isinstance(people, list) or len(people) > 32:
                raise ValueError(f"{key} must contain at most 32 players")
            cleaned = []
            identities = set()
            for person in people:
                if not isinstance(person, dict):
                    raise ValueError("voice context requires player objects")
                # Reuse identity/spatial checks without recursively validating voice.
                checked = validate_event({**person, "type": "explosion_near_player"})
                distance = person.get("distance")
                if (type(distance) not in (int, float) or not math.isfinite(distance)
                        or not 0 <= distance <= 128 or checked["dimension"] != event["dimension"]
                        or checked["player_uuid"] == event["player_uuid"]):
                    raise ValueError("invalid nearby voice player context")
                if checked["player_uuid"] in identities:
                    raise ValueError("duplicate voice player context")
                identities.add(checked["player_uuid"])
                cleaned.append({k: checked[k] for k in ("player_uuid", "player", "dimension", "position", "distance")})
            event[key] = cleaned
    if kind == "map_waypoint":
        if not isinstance(event.get("label"), str) or not re.fullmatch(r"[A-Za-z0-9 _-]{1,48}", event["label"]):
            raise ValueError("invalid waypoint label")
    return event
