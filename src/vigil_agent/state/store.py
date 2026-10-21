"""PostgreSQL state store: inbox, claims, leases, threads, transcripts, and spend.

Claims use INSERT ... ON CONFLICT DO NOTHING against a partial unique index on
the open grouping key, so concurrent intake creates exactly one investigation.
The scheduler takes an advisory lock, checks the cluster limit, and moves the
oldest eligible row to running with SELECT ... FOR UPDATE SKIP LOCKED.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import resources
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

SCHEDULER_LOCK = 7_361_001
OPEN_STATUSES = ("queued", "running", "held")
# reply_ts while the first reply is being posted; the first caller to set it posts.
PENDING = "pending"


@dataclass
class ClaimResult:
    investigation_id: int
    created: bool
    attached: bool
    was_held: bool = False
    status: str | None = None


def _j(value: Any) -> Jsonb:
    return Jsonb(json.loads(json.dumps(value, default=str)))


class Store:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10):
        self.pool = AsyncConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=False, kwargs={"row_factory": dict_row, "autocommit": True}
        )

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=30)

    async def close(self) -> None:
        await self.pool.close()

    async def migrate(self) -> None:
        sql = resources.files("vigil_agent.state").joinpath("schema.sql").read_text()
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(7361000)")
            await conn.execute(sql)

    async def fetchone(self, sql: str, params: Any = None) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Any = None) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def execute(self, sql: str, params: Any = None) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    # Inbox

    async def inbox_put(self, event_id: str, incident_id: str, source: str, payload: dict) -> bool:
        row = await self.fetchone(
            "INSERT INTO inbox (event_id, incident_id, source, payload) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (event_id) DO NOTHING RETURNING id",
            (event_id, incident_id, source, _j(payload)),
        )
        return row is not None

    @asynccontextmanager
    async def inbox_claim(self, max_attempts: int):
        """Yield the next inbox row whose incident has no older pending row, or None.

        The row stays locked while the caller works. An exception from the caller
        is recorded on the row, not raised: the row is retried with backoff, and
        after max_attempts it moves to the dead letter (item["_dead"] is True).
        """
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT * FROM inbox i WHERE status = 'pending' AND next_attempt_at <= now() "
                "AND NOT EXISTS (SELECT 1 FROM inbox j WHERE j.incident_id = i.incident_id "
                "AND j.status = 'pending' AND j.id < i.id) "
                "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
            row = await cur.fetchone()
            if row is None:
                yield None
                return
            item = dict(row)
            item["_outcome"] = {"status": "done"}
            try:
                yield item
            except Exception as exc:  # noqa: BLE001 - recorded on the row for retry
                attempts = row["attempts"] + 1
                dead = attempts >= max_attempts
                await conn.execute(
                    "UPDATE inbox SET attempts = %s, last_error = %s, status = %s, "
                    "next_attempt_at = now() + make_interval(secs => %s) WHERE id = %s",
                    (
                        attempts,
                        f"{type(exc).__name__}: {exc}"[:1000],
                        "dead" if dead else "pending",
                        min(300, 2**attempts),
                        row["id"],
                    ),
                )
                item["_dead"] = dead
                item["_error"] = exc
                return
            await conn.execute("UPDATE inbox SET status = %s WHERE id = %s", (item["_outcome"]["status"], row["id"]))

    # Events

    async def is_processed(self, event_id: str) -> bool:
        return await self.fetchone("SELECT 1 FROM processed_events WHERE event_id = %s", (event_id,)) is not None

    async def mark_processed(self, event_id: str) -> bool:
        row = await self.fetchone(
            "INSERT INTO processed_events (event_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING 1", (event_id,)
        )
        return row is not None

    # Claims

    async def claim(
        self,
        *,
        grouping_key: str,
        service: str,
        alert_name: str,
        incident_id: str | None,
        source: str,
        title: str | None = None,
        html_url: str | None = None,
        labels: dict | None = None,
        context: dict | None = None,
        early_start: bool = False,
        window_seconds: int = 300,
    ) -> ClaimResult:
        async with self.pool.connection() as conn, conn.transaction():
            if incident_id:
                cur = await conn.execute(
                    "SELECT investigation_id FROM incidents WHERE incident_id = %s", (incident_id,)
                )
                existing = await cur.fetchone()
                if existing and existing["investigation_id"]:
                    inv = await (
                        await conn.execute(
                            "SELECT status FROM investigations WHERE id = %s", (existing["investigation_id"],)
                        )
                    ).fetchone()
                    return ClaimResult(existing["investigation_id"], False, True, False, inv["status"])
            await conn.execute(
                "UPDATE investigations SET grouping_open = false WHERE grouping_key = %s AND grouping_open "
                "AND ((NOT held AND window_start < now() - make_interval(secs => %s)) "
                "OR status IN ('discarded', 'cancelled'))",
                (grouping_key, window_seconds),
            )
            cur = await conn.execute(
                "INSERT INTO investigations (grouping_key, service, alert_name, status, early_start, held, context) "
                "VALUES (%s, %s, %s, 'queued', %s, %s, %s) "
                "ON CONFLICT (grouping_key) WHERE grouping_open DO NOTHING RETURNING id",
                (grouping_key, service, alert_name, early_start, early_start, _j(context or {})),
            )
            row = await cur.fetchone()
            created = row is not None
            was_held = False
            status = "queued"
            if created:
                inv_id = row["id"]
            else:
                cur = await conn.execute(
                    "SELECT id, held, status FROM investigations WHERE grouping_key = %s AND grouping_open FOR UPDATE",
                    (grouping_key,),
                )
                found = await cur.fetchone()
                inv_id, was_held, status = found["id"], found["held"], found["status"]
                if was_held and not early_start:
                    # The page arrived for an early-start run: it becomes a real investigation.
                    await conn.execute(
                        "UPDATE investigations SET held = false, window_start = now(), intake_at = now(), "
                        "status = CASE WHEN status = 'held' THEN 'queued' ELSE status END, "
                        "context = context || %s WHERE id = %s",
                        (_j(context or {}), inv_id),
                    )
            if incident_id:
                await conn.execute(
                    "INSERT INTO incidents (incident_id, investigation_id, source, service, title, html_url, "
                    "status, labels) VALUES (%s, %s, %s, %s, %s, %s, 'triggered', %s) "
                    "ON CONFLICT (incident_id) DO UPDATE SET investigation_id = EXCLUDED.investigation_id, "
                    "status = 'triggered', updated_at = now()",
                    (incident_id, inv_id, source, service, title, html_url, _j(labels or {})),
                )
            return ClaimResult(inv_id, created, not created, was_held, status)

    # Investigations

    async def get_investigation(self, inv_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM investigations WHERE id = %s", (inv_id,))

    async def update_investigation(self, inv_id: int, owner: str | None = None, **fields: Any) -> bool:
        if not fields:
            return True
        sets = []
        params: list[Any] = []
        for k, v in fields.items():
            sets.append(f"{k} = %s")
            params.append(_j(v) if isinstance(v, dict | list) else v)
        sql = f"UPDATE investigations SET {', '.join(sets)} WHERE id = %s"  # noqa: S608 - keys are code
        params.append(inv_id)
        if owner:
            sql += " AND lease_owner = %s AND status = 'running'"
            params.append(owner)
        return await self.execute(sql, params) > 0

    async def schedule(
        self, owner: str, local_free: int, cluster_limit: int, lease_seconds: int, allow_early: bool
    ) -> list[int]:
        """Reap expired leases, then take up to the free slots. Real incidents go first."""
        if local_free <= 0:
            return []
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEDULER_LOCK,))
            await conn.execute(
                "UPDATE investigations SET status = 'queued', lease_owner = NULL "
                "WHERE status = 'running' AND lease_expires_at < now()"
            )
            running = (
                await (
                    await conn.execute("SELECT count(*) AS n FROM investigations WHERE status = 'running'")
                ).fetchone()
            )["n"]
            free = min(local_free, cluster_limit - running)
            if free <= 0:
                return []
            cur = await conn.execute(
                "SELECT id FROM investigations WHERE status = 'queued' AND (NOT held OR %s) "
                "ORDER BY held, created_at LIMIT %s FOR UPDATE SKIP LOCKED",
                (allow_early, free),
            )
            ids = [r["id"] for r in await cur.fetchall()]
            if ids:
                await conn.execute(
                    "UPDATE investigations SET status = 'running', lease_owner = %s, "
                    "lease_expires_at = now() + make_interval(secs => %s), attempts = attempts + 1, "
                    "started_at = COALESCE(started_at, now()) WHERE id = ANY(%s)",
                    (owner, lease_seconds, ids),
                )
            return ids

    async def claim_first_reply(self, inv_id: int) -> bool:
        return (
            await self.execute(
                "UPDATE investigations SET reply_ts = %s WHERE id = %s AND reply_ts IS NULL", (PENDING, inv_id)
            )
            > 0
        )

    async def running_count(self) -> int:
        return (await self.fetchone("SELECT count(*) AS n FROM investigations WHERE status = 'running'"))["n"]

    async def renew_lease(self, inv_id: int, owner: str, lease_seconds: int) -> bool:
        return (
            await self.execute(
                "UPDATE investigations SET lease_expires_at = now() + make_interval(secs => %s) "
                "WHERE id = %s AND lease_owner = %s AND status = 'running'",
                (lease_seconds, inv_id, owner),
            )
            > 0
        )

    async def finish(self, inv_id: int, owner: str, status: str, **fields: Any) -> bool:
        return await self.update_investigation(
            inv_id, owner, status=status, lease_owner=None, lease_expires_at=None, finished_at=_now_sql(), **fields
        )

    async def housekeeping(self, window_seconds: int, hold_seconds: int, retention_days: int) -> dict:
        discarded = await self.execute(
            "UPDATE investigations SET status = 'discarded', grouping_open = false, lease_owner = NULL "
            "WHERE held AND status IN ('queued', 'running', 'held') "
            "AND created_at < now() - make_interval(secs => %s)",
            (hold_seconds,),
        )
        closed = await self.execute(
            "UPDATE investigations SET grouping_open = false WHERE grouping_open AND NOT held "
            "AND window_start < now() - make_interval(secs => %s)",
            (window_seconds,),
        )
        pruned = await self.execute(
            "DELETE FROM transcripts WHERE created_at < now() - make_interval(days => %s)", (retention_days,)
        )
        await self.execute("DELETE FROM processed_events WHERE processed_at < now() - interval '14 days'")
        await self.execute(
            "DELETE FROM inbox WHERE status IN ('done', 'dropped') AND received_at < now() - interval '14 days'"
        )
        return {"discarded": discarded, "closed": closed, "pruned_transcripts": pruned}

    async def similar(self, service: str, alert_name: str, exclude_id: int, limit: int = 3) -> list[dict]:
        return await self.fetchall(
            "SELECT id, created_at, status, alert_name, COALESCE(deep_report, fast_report) AS report "
            "FROM investigations WHERE service = %s AND id <> %s AND status = 'done' "
            "ORDER BY (alert_name = %s) DESC, created_at DESC LIMIT %s",
            (service, exclude_id, alert_name, limit),
        )

    # Incidents

    async def get_incident(self, incident_id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM incidents WHERE incident_id = %s", (incident_id,))

    async def set_incident_status(self, incident_id: str, status: str) -> None:
        await self.execute(
            "UPDATE incidents SET status = %s, updated_at = now() WHERE incident_id = %s", (status, incident_id)
        )

    async def incidents_for(self, inv_id: int) -> list[dict]:
        return await self.fetchall("SELECT * FROM incidents WHERE investigation_id = %s ORDER BY created_at", (inv_id,))

    # Transcripts, spend, feedback, flaps, outbox

    async def add_transcript(self, inv_id: int, pass_: str, kind: str, payload: Any) -> None:
        await self.execute(
            "INSERT INTO transcripts (investigation_id, pass, kind, payload) VALUES (%s, %s, %s, %s)",
            (inv_id, pass_, kind, _j(payload)),
        )

    async def add_spend(self, kind: str, usd: float, day: date | None = None) -> float:
        await self.execute(
            "INSERT INTO spend (day, kind, usd) VALUES (COALESCE(%s, current_date), %s, %s) "
            "ON CONFLICT (day, kind) DO UPDATE SET usd = spend.usd + EXCLUDED.usd",
            (day, kind, usd),
        )
        return await self.spend_today(day)

    async def spend_today(self, day: date | None = None) -> float:
        row = await self.fetchone(
            "SELECT COALESCE(sum(usd), 0) AS usd FROM spend WHERE day = COALESCE(%s, current_date)", (day,)
        )
        return float(row["usd"])

    async def add_feedback(self, inv_id: int | None, rating: str, user: str | None) -> None:
        await self.execute(
            "INSERT INTO feedback (investigation_id, rating, slack_user) VALUES (%s, %s, %s)", (inv_id, rating, user)
        )

    async def incr_flap(self, grouping_key: str) -> int:
        row = await self.fetchone(
            "INSERT INTO pending_flaps (day, grouping_key, count) VALUES (current_date, %s, 1) "
            "ON CONFLICT (day, grouping_key) DO UPDATE SET count = pending_flaps.count + 1 RETURNING count",
            (grouping_key,),
        )
        return row["count"]

    async def flap_count(self, grouping_key: str) -> int:
        row = await self.fetchone(
            "SELECT count FROM pending_flaps WHERE day = current_date AND grouping_key = %s", (grouping_key,)
        )
        return row["count"] if row else 0

    async def outbox_add(
        self, action: str, channel: str, ts: str, thread_ts: str | None, text: str, blocks: list | None
    ) -> None:
        await self.execute(
            "INSERT INTO slack_outbox (action, channel, ts, thread_ts, text, blocks) VALUES (%s, %s, %s, %s, %s, %s)",
            (action, channel, ts, thread_ts, text, _j(blocks or [])),
        )


def _now_sql():
    return datetime.now(UTC)
