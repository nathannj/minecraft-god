"""Transactional shared agreements, ownership, and intervention history."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decode(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


def bounds_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    def coords(value: Any) -> list[float]:
        return [float(part) for part in value.split()] if isinstance(value, str) else list(value)
    lmin, lmax, rmin, rmax = [coords(b[key]) for b, key in [(left, "min"), (left, "max"), (right, "min"), (right, "max")]]
    return all(a <= d and c <= b for a, b, c, d in zip(lmin, lmax, rmin, rmax))


class PantheonRecords:
    """Mixin using GodLedger's per-thread SQLite connection."""

    def initialize_pantheon(self) -> None:
        self._connection().executescript("""
            CREATE TABLE IF NOT EXISTS agreements (
                id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL,
                proposer_id TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resources (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner_id TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS world_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, player_id TEXT NOT NULL,
                at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
        """)

    def propose_agreement(self, proposer_id: str, subject: str, kind: str, participants: list[str], terms: dict[str, Any], supersedes: str = "") -> dict[str, Any]:
        participants = sorted(set([proposer_id] + participants))
        if not 2 <= len(participants) <= 8 or any(not self.player(p) for p in participants):
            raise ValueError("agreements require 2 to 8 registered participants")
        if kind not in {"treaty", "shared_landmark", "joint_expedition", "feud"}:
            raise ValueError("unsupported agreement kind")
        if not subject.strip() or len(subject) > 120 or len(json.dumps(terms)) > 12000:
            raise ValueError("agreement subject or terms exceed their limits")
        if supersedes:
            prior = self.agreement(supersedes)
            if not prior or prior["status"] != "active" or prior["payload"]["participants"] != participants or prior["kind"] != kind:
                raise ValueError("a revision must name an active agreement with the same participants")
        identifier, at = uuid.uuid4().hex, now()
        payload = {"participants": participants, "accepted_by": [proposer_id], "terms": terms, "supersedes": supersedes}
        self._connection().execute("INSERT INTO agreements VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (identifier, subject.strip(), kind, proposer_id, "proposed", at, at, json.dumps(payload)))
        return self.agreement(identifier) or {}

    def agreement(self, identifier: str) -> dict[str, Any] | None:
        return decode(self._connection().execute("SELECT * FROM agreements WHERE id=?", (identifier,)).fetchone())

    def agreements_for(self, player_id: str, status: str = "") -> list[dict[str, Any]]:
        rows = self._connection().execute("SELECT * FROM agreements ORDER BY updated_at DESC").fetchall()
        return [value for row in rows if (value := decode(row)) and player_id in value["payload"]["participants"] and (not status or value["status"] == status)][:100]

    def respond_agreement(self, identifier: str, player_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"accept", "decline"}:
            raise ValueError("decision must be accept or decline")
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            value = self.agreement(identifier)
            if not value or player_id not in value["payload"]["participants"]:
                raise PermissionError("only agreement participants may respond")
            if value["status"] != "proposed":
                raise ValueError("agreement is already resolved")
            payload = value["payload"]
            accepted = set(payload["accepted_by"])
            if decision == "accept":
                accepted.add(player_id)
            payload["accepted_by"] = sorted(accepted)
            status = "declined" if decision == "decline" else "active" if accepted == set(payload["participants"]) else "proposed"
            if status == "active" and payload.get("supersedes"):
                prior = self.agreement(payload["supersedes"])
                if not prior or prior["status"] != "active":
                    raise ValueError("another revision already replaced this agreement")
                connection.execute("UPDATE agreements SET status='superseded', updated_at=? WHERE id=?", (now(), prior["id"]))
                for resource in self.resources("region"):
                    if resource["payload"].get("agreement_id") == prior["id"]:
                        connection.execute("DELETE FROM resources WHERE id=?", (resource["id"],))
            if status == "active" and value["kind"] == "shared_landmark":
                terms = payload["terms"]
                self.claim_resource("shared:" + identifier, "region", value["proposer_id"], {
                    "dimension": terms["dimension"], "bounds": terms["bounds"], "participants": payload["participants"], "agreement_id": identifier,
                })
            connection.execute("UPDATE agreements SET status=?, updated_at=?, payload_json=? WHERE id=?", (status, now(), json.dumps(payload), identifier))
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return self.agreement(identifier) or {}

    def resource(self, identifier: str) -> dict[str, Any] | None:
        return decode(self._connection().execute("SELECT * FROM resources WHERE id=?", (identifier,)).fetchone())

    def resources(self, kind: str = "") -> list[dict[str, Any]]:
        rows = self._connection().execute("SELECT * FROM resources" + (" WHERE kind=?" if kind else ""), (kind,) if kind else ()).fetchall()
        return [decode(row) for row in rows]

    def claim_resource(self, identifier: str, kind: str, owner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        connection = self._connection()
        nested = connection.in_transaction
        if not nested:
            connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.resource(identifier)
            if existing and (existing["owner_id"] != owner_id or existing["payload"].get("agreement_id")):
                raise PermissionError("resource ownership cannot be overwritten")
            participants = set(payload.get("participants", [owner_id]))
            if kind == "region":
                for other in self.resources("region"):
                    if other["id"] == identifier:
                        continue
                    theirs = other["payload"]
                    if theirs.get("dimension") == payload.get("dimension") and not set(theirs.get("participants", [other["owner_id"]])).issubset(participants):
                        if bounds_overlap(theirs["bounds"], payload["bounds"]):
                            raise PermissionError("region overlaps another god's protected location")
            connection.execute("INSERT INTO resources VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json", (identifier, kind, owner_id, json.dumps(payload)))
            if not nested:
                connection.execute("COMMIT")
        except Exception:
            if not nested and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return self.resource(identifier) or {}

    def release_resource(self, identifier: str, owner_id: str) -> None:
        value = self.resource(identifier)
        if value and value["owner_id"] == owner_id and not value["payload"].get("agreement_id"):
            self._connection().execute("DELETE FROM resources WHERE id=?", (identifier,))

    def record_world_change(self, player_id: str, change: dict[str, Any]) -> None:
        self._connection().execute("INSERT INTO world_changes(player_id,at,payload_json) VALUES (?, ?, ?)", (player_id, now(), json.dumps(change)))

    def world_changes_for(self, player_id: str) -> list[dict[str, Any]]:
        return [decode(row) for row in self._connection().execute("SELECT * FROM world_changes WHERE player_id=? ORDER BY id DESC LIMIT 12", (player_id,)).fetchall()]
