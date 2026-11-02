"""Stream bounded God transcripts into a small operational summary."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


from .runtime import workspace_root

ROOT = workspace_root()


def transcript_paths(log_dir: Path, include_archives: bool) -> list[Path]:
    current = log_dir / "god-session.jsonl"
    if not include_archives:
        return [current] if current.exists() else []
    return sorted(log_dir.glob("god-session*.jsonl"), key=lambda path: path.stat().st_mtime)


def records(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record


def summarize(paths: Iterable[Path]) -> dict[str, Any]:
    kinds: Counter[str] = Counter()
    events: Counter[str] = Counter()
    dispositions: Counter[str] = Counter()
    turns_by_player: Counter[str] = Counter()
    tool_calls: Counter[str] = Counter()
    recent = deque(maxlen=12)
    first_at = ""
    last_at = ""
    malformed_or_truncated = 0
    failures: Counter[str] = Counter()
    recent_errors = deque(maxlen=12)
    durations = deque(maxlen=2048)
    responses = deque(maxlen=2048)
    started: dict[str, str] = {}
    experience: Counter[str] = Counter()
    token_usage: Counter[str] = Counter()
    speeches: dict[str, deque[str]] = {}
    for record in records(paths):
        at = str(record.get("at", ""))
        first_at = first_at or at
        last_at = at or last_at
        kind = str(record.get("kind", "unknown"))
        kinds[kind] += 1
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        if data.get("truncated"):
            malformed_or_truncated += 1
        if kind == "turn_start":
            turns_by_player[str(data.get("player", "unknown"))] += 1
            event = data.get("event") if isinstance(data.get("event"), dict) else {}
            event_type = str(event.get("type", "idle" if data.get("idle") else "unknown"))
            events[event_type] += 1
            recent.append({"at": at, "player": data.get("player"), "event": event_type})
            for trigger in event.get("events", [event]):
                if trigger.get("type") == "player_prayer" and re.search(r"\b(?:you put|wrong|not moving|not heard|did not|didn't|why are|can't you)\b", str(trigger.get("message", "")), re.I):
                    experience["player_correction_candidates"] += 1
        elif kind == "controller_event":
            disposition = data.get("disposition")
            if disposition:
                dispositions[str(disposition)] += 1
            elif isinstance(data.get("routed_to"), dict):
                dispositions.update(str(value) for value in data["routed_to"].values())
        elif kind == "codex_notification":
            params = data.get("params") if isinstance(data.get("params"), dict) else {}
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            item_type = str(item.get("type", ""))
            if data.get("method") == "item/completed" and "tool" in item_type.casefold():
                tool_calls[str(item.get("tool") or item.get("name") or item_type)] += 1
                result = item.get("result") or {}
                structured = result.get("structuredContent") or {}
                if item.get("status") == "failed" or result.get("isError") or structured.get("ok") is False:
                    failures[str(item.get("tool"))] += 1
                    error = item.get("error") or structured.get("error")
                    if not error:
                        error = " ".join(str(content.get("text", "")) for content in result.get("content", []) if isinstance(content, dict))
                    recent_errors.append({"at": at, "tool": item.get("tool"), "error": error})
            turn = params.get("turn", {})
            if data.get("method") == "turn/started":
                started[str(turn.get("id"))] = at
            elif data.get("method") == "turn/completed":
                began = started.pop(str(turn.get("id")), None)
                if began:
                    durations.append((datetime.fromisoformat(at.replace("Z", "+00:00")) - datetime.fromisoformat(began.replace("Z", "+00:00"))).total_seconds())
        elif kind == "tool_result":
            experience["confirmed_world_changes"] += int(bool(data.get("world_changed")))
            if data.get("speech"):
                player = str(data.get("player", "unknown"))
                history = speeches.setdefault(player, deque(maxlen=32))
                normalized = re.sub(r"\s+", " ", data["speech"].casefold()).strip()
                if normalized in history:
                    experience["repeated_messages"] += 1
                if re.search(r"\b(?:owed|unfinished promise|not the rod|pray again)\b", normalized):
                    experience["reminder_candidates"] += 1
                history.append(normalized)
            if data.get("failed") and data.get("error"):
                recent_errors.append({"at": at, "player": data.get("player"), "tool": data.get("tool"), "error": data["error"]})
        elif kind == "turn_metrics":
            token_usage.update({key: value for key, value in data.get("tokens", {}).items() if isinstance(value, int)})
            if data.get("first_response_seconds") is not None:
                responses.append(float(data["first_response_seconds"]))
            experience["silent_turns"] += int(data.get("first_response_seconds") is None and not data.get("world_changes"))
        elif kind in {"turn_interrupted", "objective_achieved", "queue_drop", "session_recovery"}:
            experience[kind] += 1
    def timing(values):
        return {"samples": len(values), "median": round(statistics.median(values), 3), "max": round(max(values), 3)} if values else {"samples": 0}
    return {
        "files": [str(path) for path in paths],
        "from": first_at,
        "to": last_at,
        "record_counts": dict(kinds.most_common()),
        "turns_by_player": dict(turns_by_player.most_common()),
        "turn_trigger_types": dict(events.most_common()),
        "routing_dispositions": dict(dispositions.most_common()),
        "tool_calls": dict(tool_calls.most_common()),
        "failed_tool_calls": dict(failures.most_common()),
        "experience": dict(experience),
        "reported_tokens": dict(token_usage),
        "turn_duration_seconds": timing(durations),
        "first_response_seconds": timing(responses),
        "recent_errors": list(recent_errors),
        "truncated_records": malformed_or_truncated,
        "recent_turns": list(recent),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize Minecraft God transcript logs without loading them into memory")
    parser.add_argument("--log-dir", type=Path, default=ROOT / "logs")
    parser.add_argument("--all", action="store_true", help="Include rotated transcript archives")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()
    paths = transcript_paths(args.log_dir, args.all)
    summary = summarize(paths)
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    print(f"Transcript files: {len(paths)}")
    print(f"Range: {summary['from'] or '(empty)'} to {summary['to'] or '(empty)'}")
    for heading, key in (
        ("Turns by player", "turns_by_player"),
        ("Turn triggers", "turn_trigger_types"),
        ("Routing decisions", "routing_dispositions"),
        ("Tool calls", "tool_calls"),
        ("Failed tools", "failed_tool_calls"),
        ("Player experience", "experience"),
    ):
        print(f"\n{heading}:")
        values = summary[key]
        if not values:
            print("  (none)")
        for name, count in values.items():
            print(f"  {name}: {count}")
    print("\nRecent turns:")
    for turn in summary["recent_turns"]:
        print(f"  {turn['at']}  {turn['player']}  {turn['event']}")
    print(f"\nTurn duration: {summary['turn_duration_seconds']}")
    print(f"First response: {summary['first_response_seconds']}")
    print("\nRecent errors:")
    for error in summary["recent_errors"]:
        print(f"  {error['at']}  {error['tool']}  {error['error']}")


if __name__ == "__main__":
    main()
