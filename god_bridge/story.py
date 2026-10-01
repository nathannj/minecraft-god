"""Low-cost event policy, compaction, and story-watch matching.

This module deliberately contains no model calls.  Minecraft can be noisy;
the controller should spend tokens only after deterministic code has decided
that a player-facing decision is due.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


AUDIT_ONLY_TYPES = {"god_action", "player_quit", "player_snapshot", "block_place", "block_break", "mob_kill"}
IMMEDIATE_TYPES = {"player_prayer", "player_death", "story_milestone", "intervention_request", "intervention_response"}
MAJOR_ADVANCEMENTS = (
    "enter_the_nether", "enter_the_end", "kill_dragon", "summon_wither",
    "kill_wither", "nether/all_effects", "adventure/adventuring_time",
)
DIRECT_GOD_ADDRESS = re.compile(r"\b(?:god|gods|lord|deity|divine|heavens?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class EventDecision:
    action: str  # wake, accumulate, audit
    score: int
    reason: str


def story_is_active(state: dict[str, Any] | None) -> bool:
    state = state or {}
    phase = str(state.get("story_phase", "dormant")).casefold()
    return bool(state.get("active_thread")) or phase in {"hook", "test", "consequence"}


def event_targets(event: dict[str, Any], player_id: str, player_name: str) -> bool:
    """Apply directional delivery instead of waking every UUID in a payload."""
    policy = str(event.get("wake_policy", "")).casefold()
    event_type = str(event.get("type", "unknown")).casefold()
    player_id = player_id.casefold()
    player_name = player_name.casefold()

    if event_type == "god_action":
        return False
    if policy in {"recipient", "target"} or event_type in {"god_message", "intervention_request"}:
        return str(event.get("to_player_id") or event.get("target_player_id") or "").casefold() == player_id
    if policy == "requester" or event_type == "intervention_response":
        return str(event.get("to_player_id") or event.get("from_player_id") or "").casefold() == player_id
    if policy == "participants" or event_type == "player_death":
        return any(str(event.get(key, "")).casefold() == player_id for key in ("victim_uuid", "killer_uuid", "player_uuid"))

    if str(event.get("player_uuid", "")).casefold() == player_id:
        return True
    return bool(player_name and str(event.get("player", "")).casefold() == player_name)


def classify_event(event: dict[str, Any], state: dict[str, Any] | None = None) -> EventDecision:
    event_type = str(event.get("type", "unknown")).casefold()
    policy = str(event.get("wake_policy", "")).casefold()
    if event_type == "health_threshold":
        try:
            health = float(event.get("health", 999))
        except (TypeError, ValueError):
            health = 999
        if health <= 6:
            return EventDecision("wake", 5, "verified immediate peril")
        return EventDecision("accumulate", 2, "meaningful loss of health")
    if policy == "never" or event_type in AUDIT_ONLY_TYPES:
        return EventDecision("audit", 0, "recorded without a model turn")
    if policy == "always" or event_type in IMMEDIATE_TYPES:
        return EventDecision("wake", 5, "immediate decision event")
    if event_type in {"player_join", "god_resume"}:
        return EventDecision("wake", 5, "session opening")
    if event_type in {"chat", "player_chat"}:
        conduct = event.get("conduct") if isinstance(event.get("conduct"), dict) else {}
        if conduct.get("automatic_consequence"):
            return EventDecision("wake", 5, "automatic consequence requires acknowledgment")
        message = str(event.get("message", ""))
        if DIRECT_GOD_ADDRESS.search(message):
            return EventDecision("wake", 4, "player directly addressed the divine")
        return EventDecision("accumulate", 1, "ordinary public conversation")
    if event_type == "god_message":
        message_type = str(event.get("message_type", "intent")).casefold()
        if message_type in {"request", "warning", "approval"}:
            return EventDecision("wake", 4, f"inbound pantheon {message_type}")
        return EventDecision("accumulate", 1, "non-urgent pantheon report")
    if event_type == "advancement":
        advancement = str(event.get("advancement", "")).casefold()
        if any(marker in advancement for marker in MAJOR_ADVANCEMENTS):
            return EventDecision("wake" if story_is_active(state) else "accumulate", 3, "major advancement")
        return EventDecision("accumulate", 1, "routine advancement")
    if event_type == "dimension_change":
        return EventDecision("wake" if story_is_active(state) else "accumulate", 3, "dimension change")
    if event_type == "explosion_near_player":
        return EventDecision("wake" if story_is_active(state) else "accumulate", 2, "nearby explosion")
    return EventDecision("accumulate", 1, "unclassified player event")


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    """Remove routing/audit bulk while retaining facts needed for judgment."""
    keep = {
        "type", "at", "event_id", "player", "victim", "killer", "message",
        "private", "command", "advancement", "from", "to", "entity", "position",
        "block", "item", "amount", "health", "subject", "message_type", "verified",
        "interaction_id", "decision", "note", "conduct", "watch", "matched_event",
        "summary", "count", "inventory", "dimension",
    }
    compact = {key: value for key, value in event.items() if key in keep and value is not None and value != ""}
    if "message" in compact:
        compact["message"] = str(compact["message"])[:1000]
    if isinstance(compact.get("matched_event"), dict):
        compact["matched_event"] = compact_event(compact["matched_event"])
    return compact


def coalesce_events(events: list[dict[str, Any]], limit: int = 12) -> dict[str, Any] | None:
    if not events:
        return None
    compacted = [compact_event(event) for event in events]
    # Entering a dimension commonly emits multiple closely related advancements.
    dimensions = [event for event in compacted if event.get("type") == "dimension_change"]
    advancements = [event for event in compacted if event.get("type") == "advancement"]
    others = [event for event in compacted if event.get("type") not in {"dimension_change", "advancement"}]
    if dimensions and advancements:
        latest = dimensions[-1]
        others.append({
            "type": "journey_milestone",
            "from": latest.get("from"),
            "to": latest.get("to"),
            "advancements": [event.get("advancement") for event in advancements[-4:]],
            "summary": f"Player changed dimension with {len(advancements)} related advancement(s).",
        })
    else:
        others.extend(dimensions)
        others.extend(advancements)
    if len(others) > limit:
        omitted = len(others) - limit
        others = others[-limit:]
        others.insert(0, {"type": "event_summary", "count": omitted, "summary": f"{omitted} older low-salience events omitted."})
    if len(others) == 1:
        return others[0]
    return {"type": "event_batch", "events": others}


def _position(value: Any) -> tuple[float, float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return float(value[0]), float(value[1]), float(value[2])
        except (TypeError, ValueError):
            return None
    parts = str(value or "").replace(",", " ").split()
    if len(parts) < 3:
        return None
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None


def watch_matches(watch: dict[str, Any], event: dict[str, Any]) -> bool:
    kind = str(watch.get("kind", "event")).casefold()
    event_type = str(event.get("type", "")).casefold()
    if kind in {"all", "any"}:
        conditions = watch.get("conditions", [])
        if not isinstance(conditions, list) or not conditions:
            return False
        matches = [watch_matches(condition, event) for condition in conditions if isinstance(condition, dict)]
        return bool(matches) and (all(matches) if kind == "all" else any(matches))
    if kind == "event":
        expected = str(watch.get("event_type", "")).casefold()
        if expected and expected != event_type:
            return False
        field = str(watch.get("field", "")).strip()
        if not field:
            return bool(expected)
        actual = str(event.get(field, ""))
        if watch.get("equals") not in {None, ""}:
            return actual.casefold() == str(watch["equals"]).casefold()
        if watch.get("contains") not in {None, ""}:
            return str(watch["contains"]).casefold() in actual.casefold()
        return field in event
    if kind == "region" and event_type == "player_snapshot":
        if watch.get("dimension") and str(event.get("dimension", "")).casefold() != str(watch["dimension"]).casefold():
            return False
        current, target = _position(event.get("position")), _position(watch.get("position"))
        if not current or not target:
            return False
        radius = max(0.5, float(watch.get("radius", 3)))
        return math.dist(current, target) <= radius
    if kind == "item" and event_type in {"player_snapshot", "item_acquired"}:
        item = str(watch.get("item", "")).casefold()
        amount = max(1, int(watch.get("amount", 1)))
        if event_type == "item_acquired":
            return str(event.get("item", "")).casefold() == item and int(event.get("amount", 0)) >= amount
        inventory = event.get("inventory", {})
        return isinstance(inventory, dict) and int(inventory.get(item, inventory.get(item.removeprefix("minecraft:"), 0))) >= amount
    if kind in {"block_place", "block_break"} and event_type == kind:
        if watch.get("block") and str(event.get("block", "")).casefold() != str(watch["block"]).casefold():
            return False
        target = _position(watch.get("position"))
        actual = _position(event.get("position"))
        return not target or (actual is not None and math.dist(actual, target) <= float(watch.get("radius", 0.75)))
    if kind == "health_below" and event_type in {"player_snapshot", "health_threshold"}:
        try:
            return float(event.get("health", 999)) <= float(watch.get("health", 6))
        except (TypeError, ValueError):
            return False
    if kind == "mob_kill" and event_type == "mob_kill":
        return not watch.get("entity") or str(event.get("entity", "")).casefold() == str(watch["entity"]).casefold()
    if kind == "structure_change" and event_type in {"block_place", "block_break"}:
        if watch.get("dimension") and str(event.get("dimension", "")).casefold() != str(watch["dimension"]).casefold():
            return False
        target = _position(watch.get("position"))
        actual = _position(event.get("position"))
        if not target or not actual:
            return False
        return math.dist(actual, target) <= max(0.5, float(watch.get("radius", 8)))
    return False


def watch_deadline_due(watch: dict[str, Any], now: datetime | None = None) -> bool:
    deadline = watch.get("deadline")
    if not deadline:
        return False
    try:
        parsed = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= (now or datetime.now(timezone.utc))
