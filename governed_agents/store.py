"""Durable state: the run queue, checkpoints, the audit log and staged actions.

One schema, two backends. SQLite for local development and tests. Postgres in
production, where workers claim runs with FOR UPDATE SKIP LOCKED, so any number
of workers can poll the same table without two of them taking the same run.

A claimed run holds a lease. A worker that crashes simply stops renewing it;
once the lease expires another worker picks the run up from its last
checkpoint. After MAX_ATTEMPTS the run is dead-lettered instead of retried
forever.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

MAX_ATTEMPTS = 3

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, agent TEXT NOT NULL, trigger TEXT NOT NULL,
        dedup_key TEXT UNIQUE, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        worker TEXT, lease_until DOUBLE PRECISION, state TEXT, result TEXT,
        created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS runs_claim ON runs (status, created)",
    # Append-only. Every prompt, model reply, tool call, policy decision and approval.
    """CREATE TABLE IF NOT EXISTS events (
        run_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
        ts DOUBLE PRECISION NOT NULL, PRIMARY KEY (run_id, seq))""",
    # Draft and write tool calls wait here for a human. (run_id, call_id) makes staging idempotent,
    # so replaying a step after a crash cannot stage the same action twice.
    """CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, call_id TEXT NOT NULL, tool TEXT NOT NULL,
        args TEXT NOT NULL, status TEXT NOT NULL, result TEXT, position INTEGER NOT NULL,
        UNIQUE (run_id, call_id))""",
    # Messages to people, bundled and capped per person per day (interruption budget).
    """CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY, person TEXT NOT NULL, text TEXT NOT NULL, run_id TEXT NOT NULL,
        created DOUBLE PRECISION NOT NULL, delivered DOUBLE PRECISION)""",
]

CLAIMABLE = ("(status = 'queued' OR (status = 'running' AND lease_until < ?)) AND attempts < ?")


class Store:
    def __init__(self, url: str = "sqlite:///:memory:"):
        self.postgres = url.startswith(("postgres://", "postgresql://"))
        self._lock = threading.RLock()
        if self.postgres:
            import psycopg
            self._conn = psycopg.connect(url, autocommit=True)
        else:
            path = url.removeprefix("sqlite:///")
            self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode=WAL")
        for stmt in SCHEMA:
            self._exec(stmt)

    # --- plumbing ---
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def _exec(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._conn.execute(self._sql(sql), params)
            return cur.fetchall() if cur.description else []

    # --- runs ---
    def enqueue(self, agent: str, trigger: dict[str, Any], dedup_key: str | None = None) -> tuple[str, bool]:
        """Returns (run_id, created). A repeated webhook delivery with the same key is a no-op."""
        if dedup_key:
            row = self._exec("SELECT id FROM runs WHERE dedup_key = ?", (dedup_key,))
            if row:
                return row[0][0], False
        run_id = uuid.uuid4().hex
        now = time.time()
        try:
            self._exec("INSERT INTO runs (id, agent, trigger, dedup_key, status, created, updated) "
                       "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                       (run_id, agent, json.dumps(trigger), dedup_key, now, now))
        except Exception:  # lost a race on dedup_key: the other insert won
            row = self._exec("SELECT id FROM runs WHERE dedup_key = ?", (dedup_key,))
            if row:
                return row[0][0], False
            raise
        self.log(run_id, "enqueued", {"agent": agent, "trigger": trigger})
        return run_id, True

    def claim(self, worker: str, lease_s: float = 60.0) -> str | None:
        now = time.time()
        lock = " FOR UPDATE SKIP LOCKED" if self.postgres else ""
        rows = self._exec(
            "UPDATE runs SET status = 'running', worker = ?, lease_until = ?, attempts = attempts + 1, "
            "updated = ? WHERE id = (SELECT id FROM runs WHERE " + CLAIMABLE +
            " ORDER BY created LIMIT 1" + lock + ") RETURNING id",
            (worker, now + lease_s, now, now, MAX_ATTEMPTS))
        if rows:
            self.log(rows[0][0], "claimed", {"worker": worker})
        return rows[0][0] if rows else None

    def renew(self, run_id: str, worker: str, lease_s: float = 60.0) -> bool:
        rows = self._exec("UPDATE runs SET lease_until = ? WHERE id = ? AND worker = ? AND status = 'running' "
                          "RETURNING id", (time.time() + lease_s, run_id, worker))
        return bool(rows)

    def reap(self) -> list[str]:
        """Dead-letter runs whose lease expired on their last allowed attempt."""
        rows = self._exec("UPDATE runs SET status = 'dead', updated = ? WHERE status = 'running' "
                          "AND lease_until < ? AND attempts >= ? RETURNING id",
                          (time.time(), time.time(), MAX_ATTEMPTS))
        for (rid,) in rows:
            self.log(rid, "dead_lettered", {"attempts": MAX_ATTEMPTS})
        return [r[0] for r in rows]

    def checkpoint(self, run_id: str, worker: str, state: dict[str, Any]) -> None:
        rows = self._exec("UPDATE runs SET state = ?, updated = ? WHERE id = ? AND worker = ? RETURNING id",
                          (json.dumps(state), time.time(), run_id, worker))
        if not rows:
            raise LeaseLost(run_id)

    def finish(self, run_id: str, status: str, result: dict[str, Any] | None = None,
               worker: str | None = None) -> None:
        """Set a final (or waiting) status. A worker passes its id so a stale worker cannot overwrite."""
        fence = " AND worker = ?" if worker else ""
        rows = self._exec("UPDATE runs SET status = ?, result = ?, lease_until = NULL, updated = ? WHERE id = ?"
                          + fence + " RETURNING id",
                          (status, json.dumps(result or {}), time.time(), run_id) + ((worker,) if worker else ()))
        if not rows:
            raise LeaseLost(run_id)
        self.log(run_id, "status", {"status": status, **(result or {})})

    def transition(self, run_id: str, frm: str, to: str) -> bool:
        """Compare-and-set on status; exactly one caller wins."""
        return bool(self._exec("UPDATE runs SET status = ?, updated = ? WHERE id = ? AND status = ? RETURNING id",
                               (to, time.time(), run_id, frm)))

    def get(self, run_id: str) -> dict[str, Any] | None:
        rows = self._exec("SELECT id, agent, trigger, status, attempts, worker, state, result "
                          "FROM runs WHERE id = ?", (run_id,))
        if not rows:
            return None
        r = rows[0]
        return {"id": r[0], "agent": r[1], "trigger": json.loads(r[2]), "status": r[3], "attempts": r[4],
                "worker": r[5], "state": json.loads(r[6]) if r[6] else None,
                "result": json.loads(r[7]) if r[7] else None}

    def runs(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id FROM runs" + (" WHERE status = ?" if status else "") + " ORDER BY created"
        return [self.get(r[0]) for r in self._exec(sql, (status,) if status else ())]

    # --- audit log ---
    def log(self, run_id: str, kind: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data, default=str)
        for attempt in range(5):  # another process may take the same seq; the primary key rejects it
            with self._lock:
                seq = self._exec("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?", (run_id,))[0][0]
                try:
                    self._exec("INSERT INTO events (run_id, seq, kind, data, ts) VALUES (?, ?, ?, ?, ?)",
                               (run_id, seq, kind, payload, time.time()))
                    return
                except Exception:
                    if attempt == 4:
                        raise

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return [{"seq": s, "kind": k, "data": json.loads(d), "ts": t} for s, k, d, t in
                self._exec("SELECT seq, kind, data, ts FROM events WHERE run_id = ? ORDER BY seq", (run_id,))]

    # --- staged actions ---
    def stage(self, run_id: str, call_id: str, tool: str, args: dict[str, Any]) -> str:
        existing = self._exec("SELECT id FROM actions WHERE run_id = ? AND call_id = ?", (run_id, call_id))
        if existing:
            return existing[0][0]
        pos = self._exec("SELECT COUNT(*) FROM actions WHERE run_id = ?", (run_id,))[0][0]
        aid = uuid.uuid4().hex
        self._exec("INSERT INTO actions (id, run_id, call_id, tool, args, status, position) "
                   "VALUES (?, ?, ?, ?, ?, 'pending', ?)", (aid, run_id, call_id, tool, json.dumps(args), pos))
        return aid

    def actions(self, run_id: str) -> list[dict[str, Any]]:
        return [{"id": i, "tool": t, "args": json.loads(a), "status": s, "result": json.loads(r) if r else None}
                for i, t, a, s, r in self._exec("SELECT id, tool, args, status, result FROM actions "
                                                "WHERE run_id = ? ORDER BY position", (run_id,))]

    def set_action(self, action_id: str, status: str, result: Any = None) -> None:
        self._exec("UPDATE actions SET status = ?, result = ? WHERE id = ?",
                   (status, json.dumps(result), action_id))

    # --- outbox ---
    def queue_message(self, person: str, text: str, run_id: str) -> None:
        self._exec("INSERT INTO outbox (id, person, text, run_id, created) VALUES (?, ?, ?, ?, ?)",
                   (uuid.uuid4().hex, person, text, run_id, time.time()))

    def pending_messages(self) -> list[tuple[str, str, str]]:
        return self._exec("SELECT id, person, text FROM outbox WHERE delivered IS NULL ORDER BY created")

    def delivered_today(self, person: str) -> int:
        since = time.time() - 86400
        return self._exec("SELECT COUNT(DISTINCT delivered) FROM outbox WHERE person = ? AND delivered > ?",
                          (person, since))[0][0]

    def mark_delivered(self, ids: list[str], at: float) -> None:
        for i in ids:
            self._exec("UPDATE outbox SET delivered = ? WHERE id = ?", (at, i))


class LeaseLost(Exception):
    """Another worker took the run over (our lease expired). Stop without writing anything."""
