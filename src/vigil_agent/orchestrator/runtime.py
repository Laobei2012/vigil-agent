"""Background loops of the orchestrator pod.

- The intake worker drains the PostgreSQL inbox (or SQS in production).
- The scheduler reaps expired leases and starts queued investigations, real
  incidents before early-start runs, within the cluster and pod limits.
- Each running investigation renews its lease every 30 s. A pod that loses its
  lease stops; the pod that takes the row resumes from the last saved step.
- The early-start poller claims pending alerts so the fast pass runs before
  the alert's for: window ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time

import httpx

from vigil_agent import telemetry
from vigil_agent.config import Settings
from vigil_agent.intake.events import Alert, IntakeEvent
from vigil_agent.orchestrator.intake import Intake, grouping_key
from vigil_agent.orchestrator.investigation import Investigator, LeaseLost
from vigil_agent.orchestrator.publisher import Publisher
from vigil_agent.state.store import Store

log = logging.getLogger("vigil.runtime")


class Runtime:
    def __init__(self, settings: Settings, store: Store, intake: Intake, investigator: Investigator,
                 publisher: Publisher, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.store = store
        self.intake = intake
        self.investigator = investigator
        self.pub = publisher
        self.http = http or httpx.AsyncClient(timeout=10)
        self.tasks: dict[int, asyncio.Task] = {}
        self._loops: list[asyncio.Task] = []
        self._pending_seen: dict[str, dict] = {}
        self._stopping = False

    # Intake

    async def drain_inbox_once(self) -> bool:
        async with self.store.inbox_claim(self.s.intake_max_attempts) as item:
            if item is None:
                return False
            event = IntakeEvent.model_validate(item["payload"])
            result = await self.intake.handle(event)
            if result in ("other_cluster", "service_not_enabled", "unmapped_service", "kill_switch",
                          "duplicate", "unknown_incident"):
                item["_outcome"]["status"] = "dropped"
        if item.get("_dead"):
            telemetry.INTAKE_DEAD.inc()
            await self.pub.health(f":warning: vigil-agent intake gave up on event {item['event_id']} "
                                  f"(incident {item['incident_id']}) after {self.s.intake_max_attempts} attempts: "
                                  f"{item.get('_error')}. PagerDuty paging is not affected.")
        return True

    async def intake_loop(self) -> None:
        while not self._stopping:
            try:
                if not await self.drain_inbox_once():
                    await asyncio.sleep(0.5)
            except Exception:
                log.exception("intake loop error")
                await asyncio.sleep(2)

    async def sqs_loop(self, client, queue_url: str) -> None:
        """Production intake. A message is deleted only after intake succeeds; otherwise SQS redelivers
        it, and after the maximum receive count the redrive policy moves it to the DLQ."""
        while not self._stopping:
            try:
                resp = await asyncio.to_thread(client.receive_message, QueueUrl=queue_url,
                                               MaxNumberOfMessages=10, WaitTimeSeconds=20)
            except Exception:
                log.exception("SQS receive failed")
                await asyncio.sleep(5)
                continue
            for m in resp.get("Messages", []):
                try:
                    await self.intake.handle(IntakeEvent.model_validate_json(m["Body"]))
                except Exception:
                    log.exception("intake failed; SQS will redeliver message %s", m.get("MessageId"))
                    continue
                await asyncio.to_thread(client.delete_message, QueueUrl=queue_url,
                                        ReceiptHandle=m["ReceiptHandle"])

    # Scheduler

    async def allow_early(self) -> bool:
        if not self.s.early_start_enabled:
            return False
        if await self.store.running_count() >= self.s.max_running_cluster:
            return False
        return await self.store.spend_today() < self.s.daily_spend_cap_usd / 2

    async def schedule_once(self) -> list[int]:
        self.tasks = {k: t for k, t in self.tasks.items() if not t.done()}
        telemetry.INVESTIGATIONS_RUNNING.set(len(self.tasks))
        free = self.s.max_running_pod - len(self.tasks)
        ids = await self.store.schedule(self.s.owner, free, self.s.max_running_cluster, self.s.lease_seconds,
                                        await self.allow_early())
        for inv_id in ids:
            self.tasks[inv_id] = asyncio.create_task(self.run_with_lease(inv_id), name=f"investigation-{inv_id}")
        return ids

    async def scheduler_loop(self) -> None:
        last_housekeeping = 0.0
        while not self._stopping:
            try:
                if time.monotonic() - last_housekeeping > 60:
                    out = await self.store.housekeeping(self.s.grouping_window_seconds, self.s.early_start_hold_seconds,
                                                        self.s.transcript_retention_days)
                    if any(out.values()):
                        log.info("housekeeping %s", out)
                    last_housekeeping = time.monotonic()
                await self.schedule_once()
            except Exception:
                log.exception("scheduler loop error")
            await asyncio.sleep(self.s.scheduler_interval_seconds)

    async def run_with_lease(self, inv_id: int) -> None:
        main = asyncio.create_task(self.investigator.run(inv_id))

        async def heartbeat():
            while not main.done():
                await asyncio.sleep(self.s.lease_renew_seconds)
                if not await self.store.renew_lease(inv_id, self.s.owner, self.s.lease_seconds):
                    log.warning("investigation %s: lease lost, stopping", inv_id)
                    main.cancel()
                    return

        hb = asyncio.create_task(heartbeat())
        try:
            await main
        except (asyncio.CancelledError, LeaseLost):
            log.warning("investigation %s stopped on this pod", inv_id)
        except Exception as exc:
            log.exception("investigation %s failed", inv_id)
            inv = await self.store.get_investigation(inv_id)
            if inv and inv["lease_owner"] == self.s.owner:
                await self.store.finish(inv_id, self.s.owner, "failed")
                with contextlib.suppress(Exception):
                    await self.pub.reply(inv_id, f":warning: Vigil could not finish this investigation "
                                         f"({type(exc).__name__}). Use the runbook; paging is not affected.")
        finally:
            hb.cancel()

    # Early start

    async def poll_pending_once(self) -> int:
        resp = await self.http.get(f"{self.s.pending_alerts_url.rstrip('/')}/api/v1/alerts")
        resp.raise_for_status()
        alerts = resp.json().get("data", {}).get("alerts", [])
        pending = {}
        for a in alerts:
            if a.get("state") != "pending":
                continue
            labels = a.get("labels", {})
            key = json.dumps(labels, sort_keys=True)
            pending[key] = a
        firing = {json.dumps(a.get("labels", {}), sort_keys=True) for a in alerts if a.get("state") == "firing"}
        catalog = self.intake.catalog
        # Pending alerts that went back to inactive count as flaps.
        for key, a in self._pending_seen.items():
            if key not in pending and key not in firing:
                entry = catalog.by_namespace(a.get("labels", {}).get("namespace"))
                if entry:
                    await self.store.incr_flap(grouping_key(entry.name, a["labels"].get("alertname", "")))
        self._pending_seen = pending
        if not await self.allow_early():
            return 0
        claimed = 0
        for key, a in pending.items():
            labels = a.get("labels", {})
            entry = catalog.by_namespace(labels.get("namespace"))
            if entry is None or not entry.vigil_enabled or labels.get("vigil_health") == "true":
                continue
            gk = grouping_key(entry.name, labels.get("alertname", ""))
            if await self.store.flap_count(gk) > self.s.early_start_flap_limit:
                continue
            event_id = "es-" + hashlib.sha256(f"{key}|{a.get('activeAt')}".encode()).hexdigest()[:24]
            event = IntakeEvent(source="early_start", event_id=event_id,
                                event_type="incident.triggered", incident_id=f"es-{gk}",
                                title=f"{labels.get('alertname')}: {a.get('annotations', {}).get('summary', '')}",
                                urgency="high" if labels.get("severity") == "critical" else "low",
                                alerts=[Alert(labels=labels, annotations=a.get("annotations", {}), status="pending",
                                              starts_at=a.get("activeAt"))])
            if await self.intake.handle(event) == "early_start_claimed":
                claimed += 1
        return claimed

    async def early_start_loop(self) -> None:
        while not self._stopping:
            try:
                await self.poll_pending_once()
            except Exception:
                log.exception("early start poll failed")
            await asyncio.sleep(self.s.early_start_poll_seconds)

    def start(self, intake: bool = True) -> None:
        if intake:
            self._loops.append(asyncio.create_task(self.intake_loop(), name="intake"))
        self._loops.append(asyncio.create_task(self.scheduler_loop(), name="scheduler"))
        if self.s.early_start_enabled and self.s.pending_alerts_url:
            self._loops.append(asyncio.create_task(self.early_start_loop(), name="early-start"))

    async def stop(self) -> None:
        self._stopping = True
        for t in self._loops:
            t.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
