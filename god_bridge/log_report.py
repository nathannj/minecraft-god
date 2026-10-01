"""Stream bounded God transcripts into a small operational summary."""

from __future__ import annotations

import argparse
import json
from collections import Counter, deque
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]


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
            if "tool" in item_type.casefold():
                tool_calls[str(item.get("tool") or item.get("name") or item_type)] += 1
    return {
        "files": [str(path) for path in paths],
        "from": first_at,
        "to": last_at,
        "record_counts": dict(kinds.most_common()),
        "turns_by_player": dict(turns_by_player.most_common()),
        "turn_trigger_types": dict(events.most_common()),
        "routing_dispositions": dict(dispositions.most_common()),
        "tool_calls": dict(tool_calls.most_common()),
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


if __name__ == "__main__":
    main()
