"""Replay event routing and objective recognition without Minecraft or model calls."""
from __future__ import annotations

import argparse
import json
import queue
import tempfile
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from .controller import GodSession, Transcript, _event_for_turn
from .story import event_targets
from .runtime import profile_path


class OfflineAppServer:
    def __init__(self, *_):
        self.notifications = queue.Queue()
        self.requests = []
        self.process = type("OfflineProcess", (), {"poll": lambda _: None})()

    def request(self, method: str, params: dict[str, Any] | None = None, **_):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "offline-replay"}}
        if method == "turn/start":
            return {"turn": {"id": "offline-turn"}}
        return {}

    def notify(self, *_):
        pass

    def close(self):
        pass


def replay(events, player: str, player_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="minecraft-god-replay-") as directory:
        root = Path(directory)
        profile = profile_path(root / "state" / "players", player_id)
        profile.mkdir(parents=True)
        (profile / "god_state.json").write_text(json.dumps(state or {}), encoding="utf-8")
        session = GodSession(player_id, player, root, Transcript(root), 120, app_server_factory=OfflineAppServer, start_worker=False)
        decisions = Counter()
        milestones = []
        for event in events:
            if not event_targets(event, player_id, player):
                continue
            decisions[session.ingest(event)] += 1
            pending = _event_for_turn(session._pending)
            session._pending_event_ids.clear()
            if pending:
                for item in pending.get("events", [pending]):
                    if item.get("type") == "story_milestone":
                        milestones.append(item)
        presence = session._presence_pulse() if session._ambient else None
        final_state = session._state()
        session.close()
        return {"routing": dict(decisions), "milestones": milestones, "remaining_presence": presence, "state": final_state}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--player", required=True)
    parser.add_argument("--player-id", default="")
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    identifier = args.player_id or str(uuid.uuid5(uuid.NAMESPACE_DNS, "replay:" + args.player))
    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state else {}
    def events():
        with args.events.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    print(json.dumps(replay(events(), args.player, identifier, state), indent=2))


if __name__ == "__main__":
    main()
