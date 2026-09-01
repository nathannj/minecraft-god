"""Small SQLite ledger shared by the event hub and every god process.

The MCP bridge is launched once for each Codex god, so in-memory ownership
checks are not enough. This ledger is the durable, transactional authority for
player identities and god-to-god requests.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    try:
        result["payload"] = json.loads(result.pop("payload_json") or "{}")
    except json.JSONDecodeError:
        result["payload"] = {}
    return result


class GodLedger:
    """Cross-process registry and capability ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            self._local.connection = connection
        return connection

    def _initialize(self) -> None:
        connection = self._connection()
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                player_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                online INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS players_name_ci ON players(lower(name));

            CREATE TABLE IF NOT EXISTS interactions (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                from_player_id TEXT NOT NULL,
                to_player_id TEXT NOT NULL,
                action TEXT,
                target_player_id TEXT,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                event_id INTEGER,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                used_at TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS interactions_to_status ON interactions(to_player_id, status, created_at);
            CREATE INDEX IF NOT EXISTS interactions_from_status ON interactions(from_player_id, status, created_at);

            CREATE TABLE IF NOT EXISTS sequences (
                name TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            """
        )

    def next_event_id(self, minimum: int = 0) -> int:
        """Allocate a globally unique event id across all bridge processes."""
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT value FROM sequences WHERE name='events'").fetchone()
            current = int(row[0]) if row else 0
            current = max(current, int(minimum)) + 1
            connection.execute(
                "INSERT INTO sequences(name, value) VALUES('events', ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (current,),
            )
            connection.execute("COMMIT")
            return current
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def register_player(self, player_id: str, name: str, *, online: bool = True) -> dict[str, Any]:
        player_id = str(player_id).strip().lower()
        name = str(name).strip()
        if not player_id or not name:
            raise ValueError("player_id and name are required")
        now = utc_now()
        connection = self._connection()
        connection.execute(
            """
            INSERT INTO players(player_id, name, first_seen, last_seen, online)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen, online=excluded.online
            """,
            (player_id, name, now, now, int(online)),
        )
        return self.player(player_id) or {"player_id": player_id, "name": name, "online": online}

    def set_online(self, player_id: str, online: bool) -> dict[str, Any] | None:
        connection = self._connection()
        connection.execute("UPDATE players SET online=?, last_seen=? WHERE player_id=?", (int(online), utc_now(), player_id.lower()))
        return self.player(player_id)

    def player(self, reference: str) -> dict[str, Any] | None:
        reference = str(reference).strip()
        if not reference:
            return None
        connection = self._connection()
        row = connection.execute(
            "SELECT player_id, name, first_seen, last_seen, online FROM players WHERE player_id=? OR lower(name)=lower(?) LIMIT 1",
            (reference.lower(), reference),
        ).fetchone()
        return dict(row) if row else None

    def players(self) -> list[dict[str, Any]]:
        rows = self._connection().execute("SELECT player_id, name, first_seen, last_seen, online FROM players ORDER BY lower(name)").fetchall()
        return [dict(row) for row in rows]

    def add_interaction(
        self,
        *,
        kind: str,
        from_player_id: str,
        to_player_id: str,
        action: str = "",
        target_player_id: str = "",
        status: str = "pending",
        message: str = "",
        reason: str = "",
        event_id: int | None = None,
        expires_at: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        interaction_id = uuid.uuid4().hex
        created_at = utc_now()
        self._connection().execute(
            """
            INSERT INTO interactions(id, kind, from_player_id, to_player_id, action, target_player_id, status,
                                     message, reason, event_id, expires_at, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (interaction_id, kind, from_player_id.lower(), to_player_id.lower(), action, target_player_id.lower(), status,
             message, reason, event_id, expires_at, created_at, json.dumps(payload or {}, separators=(",", ":"))),
        )
        return self.interaction(interaction_id) or {}

    def interaction(self, interaction_id: str) -> dict[str, Any] | None:
        row = self._connection().execute("SELECT * FROM interactions WHERE id=?", (str(interaction_id),)).fetchone()
        return _decode(row)

    def interactions_for(self, player_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT * FROM interactions WHERE from_player_id=? OR to_player_id=? ORDER BY created_at DESC LIMIT ?",
            (player_id.lower(), player_id.lower(), max(1, min(int(limit), 100))),
        ).fetchall()
        return [_decode(row) for row in rows if row is not None]

    def respond(self, interaction_id: str, player_id: str, decision: str, note: str = "") -> dict[str, Any]:
        decision = decision.lower().strip()
        if decision not in {"approve", "decline"}:
            raise ValueError("decision must be approve or decline")
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT * FROM interactions WHERE id=?", (interaction_id,)).fetchone()
            interaction = _decode(row)
            if not interaction:
                raise ValueError("intervention request was not found")
            if interaction["to_player_id"] != player_id.lower():
                raise PermissionError("only the target player's god may answer this request")
            if interaction["kind"] != "intervention_request" or interaction["status"] != "pending":
                raise ValueError(f"request is already {interaction['status']}")
            if interaction.get("expires_at") and interaction["expires_at"] <= utc_now():
                connection.execute("UPDATE interactions SET status='expired', resolved_at=? WHERE id=?", (utc_now(), interaction_id))
                connection.execute("COMMIT")
                raise PermissionError("intervention request has expired")
            status = "approved" if decision == "approve" else "declined"
            resolved_at = utc_now()
            payload = dict(interaction.get("payload") or {})
            payload["response_note"] = note
            connection.execute(
                "UPDATE interactions SET status=?, resolved_at=?, message=?, payload_json=? WHERE id=?",
                (status, resolved_at, note, json.dumps(payload, separators=(",", ":")), interaction_id),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return self.interaction(interaction_id) or {}

    def consume(self, interaction_id: str, player_id: str) -> dict[str, Any]:
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT * FROM interactions WHERE id=?", (interaction_id,)).fetchone()
            interaction = _decode(row)
            if not interaction:
                raise ValueError("intervention request was not found")
            if interaction["from_player_id"] != player_id.lower():
                raise PermissionError("only the requesting god may use this permission")
            if interaction["status"] != "approved":
                raise PermissionError(f"intervention is not approved; current status is {interaction['status']}")
            expiry = interaction.get("expires_at")
            if expiry and expiry <= utc_now():
                connection.execute("UPDATE interactions SET status='expired', resolved_at=? WHERE id=?", (utc_now(), interaction_id))
                connection.execute("COMMIT")
                raise PermissionError("intervention permission has expired")
            used_at = utc_now()
            connection.execute("UPDATE interactions SET status='used', used_at=? WHERE id=? AND status='approved'", (used_at, interaction_id))
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return self.interaction(interaction_id) or {}


def expiry_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))).isoformat(timespec="seconds")
