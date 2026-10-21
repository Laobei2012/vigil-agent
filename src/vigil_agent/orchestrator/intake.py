"""Intake: dedupe, gates, cluster routing, claims, and thread updates for status events.

Intake takes seconds and never runs the investigation itself. A new incident
gets a queued investigation and a Slack placeholder; the scheduler starts it.
Ack and resolve events update the record and the thread directly.
"""

from __future__ import annotations

import logging

from vigil_agent import telemetry
from vigil_agent.agent.report import Report
from vigil_agent.catalog import Catalog, Flags, ReloadingFile, ServiceEntry
from vigil_agent.config import Settings
from vigil_agent.intake.events import IntakeEvent
from vigil_agent.orchestrator.publisher import Publisher
from vigil_agent.pagerduty import PagerDutyClient
from vigil_agent.state.store import Store

log = logging.getLogger("vigil.intake")


def route(event: IntakeEvent, cluster: str, primary: bool) -> tuple[bool, str]:
    """Keep the event if one of its alerts carries this cluster's label.

    Events with no cluster label at all go to the primary cluster only.
    """
    clusters = {a.labels.get("cluster") for a in event.alerts if a.labels.get("cluster")}
    if cluster in clusters:
        event.alerts = [a for a in event.alerts if a.labels.get("cluster") in (cluster, None)]
        return True, "cluster_label"
    if clusters:
        return False, "other_cluster"
    return (True, "primary_cluster") if primary else (False, "no_cluster_label_not_primary")


def grouping_key(service: str, alert_name: str) -> str:
    return f"{service}|{alert_name}"


class Intake:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        catalog: ReloadingFile,
        flags: ReloadingFile,
        pd: PagerDutyClient,
        publisher: Publisher,
    ):
        self.s = settings
        self.store = store
        self.catalog_file = catalog
        self.flags_file = flags
        self.pd = pd
        self.pub = publisher

    @property
    def catalog(self) -> Catalog:
        return self.catalog_file.get()

    @property
    def flags(self) -> Flags:
        return self.flags_file.get()

    def resolve_service(self, event: IntakeEvent) -> ServiceEntry | None:
        # The namespace is more specific than the PagerDuty service: one PagerDuty
        # service can page for many workloads, as in a cluster with one service.
        return self.catalog.by_namespace(event.label("namespace")) or self.catalog.get(event.service_id)

    async def handle(self, event: IntakeEvent) -> str:
        result = await self._handle(event)
        telemetry.INTAKE_EVENTS.labels(source=event.source, result=result).inc()
        log.info("intake %s %s %s: %s", event.source, event.event_type, event.incident_id, result)
        return result

    async def _handle(self, event: IntakeEvent) -> str:
        if await self.store.is_processed(event.event_id):
            return "duplicate"
        if event.event_type in ("incident.acknowledged", "incident.resolved"):
            result = await self._status(event)
        else:
            result = await self._trigger(event)
        await self.store.mark_processed(event.event_id)
        return result

    async def _trigger(self, event: IntakeEvent) -> str:
        gaps: list[str] = []
        if event.source == "pagerduty":
            if self.pd.enabled:
                # Raises after retries: the inbox retries the event, then dead-letters it.
                event.alerts = await self.pd.incident_alerts(event.incident_id)
            else:
                gaps.append(
                    "PagerDuty API key not configured: alert labels unknown, routed by the primary-cluster rule"
                )
        if any(a.labels.get("vigil_health") == "true" for a in event.alerts):
            return "agent_health_alert"
        keep, why = route(event, self.s.cluster, self.s.primary_cluster)
        if not keep:
            return why
        entry = self.resolve_service(event)
        if entry is None and not self.catalog.defaults.unmapped_enabled:
            return "unmapped_service"
        if entry is not None and not entry.vigil_enabled:
            return "service_not_enabled"
        service = entry.name if entry else f"unmapped:{event.label('namespace') or 'none'}"
        if not self.flags.allows(self.s.cluster, service):
            return "kill_switch"
        if entry and entry.low_urgency_skip and event.urgency == "low":
            return "low_urgency_skipped"
        alert_name = event.label("alertname") or event.title.split(":")[0][:100]
        key = grouping_key(service, alert_name)
        context = {
            "title": event.title,
            "urgency": event.urgency,
            "html_url": event.html_url,
            "source": event.source,
            "incident_id": event.incident_id,
            "service_id": event.service_id,
            "channel": self.catalog.channel_for(entry),
            "alerts": [a.model_dump() for a in event.alerts],
            "namespace": event.label("namespace"),
            "gaps": gaps,
        }
        existing = None if event.source == "early_start" else await self.store.get_incident(event.incident_id)
        reopened = existing is not None and (
            event.event_type == "incident.reopened" or existing["status"] == "resolved"
        )
        old_inv = None
        if reopened:
            old_inv = await self.store.get_investigation(existing["investigation_id"])
            await self.store.execute("UPDATE investigations SET grouping_open = false WHERE id = %s", (old_inv["id"],))
            await self.store.execute(
                "UPDATE incidents SET investigation_id = NULL WHERE incident_id = %s", (event.incident_id,)
            )
        claim = await self.store.claim(
            grouping_key=key,
            service=service,
            alert_name=alert_name,
            incident_id=None if event.source == "early_start" else event.incident_id,
            source=event.source,
            title=event.title,
            html_url=event.html_url,
            labels=event.alerts[0].labels if event.alerts else {},
            context=context,
            early_start=event.source == "early_start",
            window_seconds=self.s.grouping_window_seconds,
        )
        if event.source == "early_start":
            return "early_start_claimed" if claim.created else "early_start_exists"
        if claim.created:
            if reopened and old_inv and old_inv["slack_ts"]:
                await self.store.update_investigation(
                    claim.investigation_id, slack_channel=old_inv["slack_channel"], slack_ts=old_inv["slack_ts"]
                )
                await self.pub.reply(claim.investigation_id, ":repeat: Incident reopened. Investigating again.")
                await self.pub.refresh_parent(claim.investigation_id)
                return "reopened"
            await self.pub.ensure_parent(claim.investigation_id)
            return "claimed"
        if claim.was_held:
            return await self._release_held(claim.investigation_id, claim.status)
        if existing is not None:
            # A redelivered event: an earlier attempt can have stopped before the
            # placeholder was posted, so make sure that the thread exists.
            await self.pub.ensure_parent(claim.investigation_id)
            return "already_attached"
        await self.pub.ensure_parent(claim.investigation_id)
        await self.pub.reply(
            claim.investigation_id,
            f":link: {event.title} ({event.incident_id}) is grouped into "
            "this investigation: same service and alert within 5 minutes.",
        )
        await self.pub.refresh_parent(claim.investigation_id)
        return "attached"

    async def _release_held(self, inv_id: int, status_before: str | None) -> str:
        """The page arrived for an early-start run: post its held result at once."""
        inv = await self.pub.ensure_parent(inv_id)
        if status_before != "held" or not await self.store.claim_first_reply(inv_id):
            # Still running: the run posts its own result when it ends.
            return "early_start_released"
        prefetch = inv["prefetch"] or {}
        evidence = prefetch.get("evidence", {})
        footer = f"early start: prepared while the alert was pending | transcript: investigation {inv_id}"
        if inv["fast_report"]:
            report = Report.model_validate(inv["fast_report"])
            await self.pub.post_report(inv_id, report, evidence, "First pass (early start)", footer)
        else:
            reason = "Prepared while the alert was pending, without model analysis."
            await self.pub.post_evidence_only(inv_id, prefetch, evidence, reason, footer)
            await self.pub.refresh_parent(inv_id, verdict="evidence only, see the thread")
        return "early_start_released"

    async def _status(self, event: IntakeEvent) -> str:
        incident = await self.store.get_incident(event.incident_id)
        if incident is None or incident["investigation_id"] is None:
            return "unknown_incident"
        status = "acknowledged" if event.event_type == "incident.acknowledged" else "resolved"
        if incident["status"] == "resolved" and status == "acknowledged":
            return "stale_ack"
        await self.store.set_incident_status(event.incident_id, status)
        inv_id = incident["investigation_id"]
        inv = await self.store.get_investigation(inv_id)
        icon = ":eyes:" if status == "acknowledged" else ":white_check_mark:"
        await self.pub.reply(
            inv_id,
            f"{icon} {incident['title'] or event.incident_id}: {status} in "
            f"{'PagerDuty' if event.source == 'pagerduty' else 'Alertmanager'}.",
        )
        incidents = await self.store.incidents_for(inv_id)
        if status == "resolved" and all(i["status"] == "resolved" for i in incidents):
            await self.store.update_investigation(inv_id, resolved=True)
            if inv["status"] == "queued":
                await self.store.update_investigation(inv_id, status="cancelled", grouping_open=False)
                await self.pub.reply(inv_id, "Resolved before the investigation started. No report.")
            elif inv["status"] == "done":
                verdict = (inv["deep_report"] or inv["fast_report"] or {}).get("verdict")
                await self.pub.reply(
                    inv_id, f":memo: Recap: {verdict or 'no model analysis'}. The report above can seed the postmortem."
                )
        await self.pub.refresh_parent(inv_id)
        return status
