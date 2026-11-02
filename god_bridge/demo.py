"""Replay a synthetic quest without Minecraft, Codex, credentials, or network."""
from __future__ import annotations

import argparse
from importlib.resources import files
import json

from .events import validate_event
from .replay import replay
from .story import event_targets


def run_demo() -> dict:
    scenario = json.loads(files("god_bridge").joinpath("demo_scenario.json").read_text(encoding="utf-8"))
    events = [validate_event(event) for event in scenario["events"]]
    result = replay(events, scenario["player"], scenario["player_id"], scenario["state"])
    return {
        "scenario": scenario["description"],
        "events": len(events),
        "other_player_events_ignored": sum(not event_targets(event, scenario["player_id"], scenario["player"]) for event in events),
        "routing": result["routing"],
        "milestones": len(result["milestones"]),
        "objective_status": result["state"]["story_objectives"][0]["status"],
        "model_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the deterministic result as JSON")
    args = parser.parse_args()
    result = run_demo()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("MINECRAFT GOD / OFFLINE DEMO")
        print(result["scenario"])
        print(f"{result['events']} events replayed; {result['other_player_events_ignored']} other-player event ignored.")
        print(f"{result['milestones']} objective milestone detected, without a second prayer.")
        print("Status: " + result["objective_status"])
        print("The controller recognized the goal. A live god would judge the outcome next.")
        print("Model calls: 0. Temporary replay data was removed.")
    if result["milestones"] != 1 or result["objective_status"] != "achieved_pending_judgment":
        raise SystemExit("Demo regression: the objective was not recognized exactly once")


if __name__ == "__main__":
    main()
