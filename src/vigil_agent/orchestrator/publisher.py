"""One Slack thread per investigation: a short parent message and replies.

The parent shows severity, service, cluster, title, status, and a one-line
verdict. The first reply is the fast pass (or the evidence-only reply). The
deep pass posts a further reply and marks the first reply as updated; it never
overwrites the fast-pass text.
"""

from __future__ import annotations

import logging

from vigil_agent import telemetry
from vigil_agent.agent.report import Report
from vigil_agent.config import Settings
from vigil_agent.slack.client import SlackPoster, post_blocks
from vigil_agent.slack.render import (
    evidence_only_blocks,
    parent_blocks,
    plain_text,
    report_blocks,
    sanitize,
)
from vigil_agent.state.store import PENDING, Store

log = logging.getLogger("vigil.publisher")


class Publisher:
    def __init__(self, settings: Settings, store: Store, slack: SlackPoster):
        self.s = settings
        self.store = store
        self.slack = slack

    @property
    def allowed(self) -> list[str]:
        return self.s.allowed_link_domains

    async def _parent(self, inv: dict, status: str | None = None, verdict: str | None = None):
        ctx = inv["context"]
        incidents = await self.store.incidents_for(inv["id"])
        if status is None:
            statuses = {i["status"] for i in incidents}
            status = ("resolved" if statuses == {"resolved"} else
                      "acknowledged" if "acknowledged" in statuses else "triggered")
        title = ctx.get("title") or inv["alert_name"]
        if len(incidents) > 1:
            title += f" (+{len(incidents) - 1} grouped)"
        return parent_blocks(severity=ctx.get("urgency", "high"), service=inv["service"], cluster=self.s.cluster,
                             title=f"[{self.s.cluster}] {title}", pd_url=ctx.get("html_url"), status=status,
                             verdict=verdict or ctx.get("verdict"), allowed=self.allowed)

    async def ensure_parent(self, inv_id: int) -> dict:
        inv = await self.store.get_investigation(inv_id)
        if inv["slack_ts"]:
            return inv
        channel = inv["context"].get("channel") or self.s.slack_fallback_channel
        text, blocks = await self._parent(inv)
        ts = await self.slack.post(channel, text, blocks)
        await self.store.update_investigation(inv_id, slack_channel=channel, slack_ts=ts)
        return await self.store.get_investigation(inv_id)

    async def refresh_parent(self, inv_id: int, verdict: str | None = None) -> None:
        inv = await self.store.get_investigation(inv_id)
        if not inv["slack_ts"]:
            return
        if verdict:
            ctx = dict(inv["context"])
            ctx["verdict"] = verdict
            await self.store.update_investigation(inv_id, context=ctx)
            inv["context"] = ctx
        text, blocks = await self._parent(inv)
        await self.slack.update(inv["slack_channel"], inv["slack_ts"], text, blocks)

    async def reply(self, inv_id: int, text: str) -> str:
        inv = await self.ensure_parent(inv_id)
        clean = sanitize(text, self.allowed)
        return await self.slack.post(inv["slack_channel"], clean,
                                     [{"type": "section", "text": {"type": "mrkdwn", "text": clean[:3000]}}],
                                     inv["slack_ts"])

    def footer(self, *, model: str | None, tool_calls: int, seconds: float, inv_id: int, cost: float | None) -> str:
        parts = [f"model: {model or 'none'}", f"tool calls: {tool_calls}", f"duration: {seconds:.0f}s",
                 f"transcript: investigation {inv_id} (vigil_agent.transcripts)"]
        if cost is not None:
            parts.append(f"est. cost: ${cost:.2f}")
        return " | ".join(parts)

    async def post_report(self, inv_id: int, report: Report, evidence: dict, label: str, footer: str) -> str:
        inv = await self.ensure_parent(inv_id)
        blocks = report_blocks(report, evidence, allowed=self.allowed, footer=footer, label=label,
                               investigation_id=inv_id, buttons=bool(self.s.slack_app_token))
        ts = (await post_blocks(self.slack, inv["slack_channel"], f"{label}: {report.verdict}", blocks,
                                inv["slack_ts"]))[0]
        telemetry.REPORTS.labels(outcome=report.mode).inc()
        if report.mode == "fast" or inv["reply_ts"] in (None, PENDING):
            await self.store.update_investigation(inv_id, reply_ts=ts)
        await self.refresh_parent(inv_id, verdict=report.verdict)
        return ts

    async def mark_first_reply_updated(self, inv_id: int, fast: Report, deep: Report, evidence: dict,
                                       footer: str) -> None:
        """Edit the first reply to say what the deep pass changed, keeping the fast-pass text."""
        inv = await self.store.get_investigation(inv_id)
        if inv["reply_ts"] in (None, PENDING):
            return
        changes = ", ".join(f"{h.cause[:60]}: {h.status.replace('_', ' ')}" for h in deep.hypotheses
                            if h.status != "new") or "see the deep-pass reply"
        blocks = report_blocks(fast, evidence, allowed=self.allowed, footer=footer,
                               label="First pass (updated by the deep pass below)", investigation_id=inv_id,
                               buttons=False)
        blocks.insert(1, {"type": "context", "elements": [{"type": "mrkdwn", "text": sanitize(
            f":arrows_counterclockwise: Deep pass: {changes}", self.allowed)[:3000]}]})
        await self.slack.update(inv["slack_channel"], inv["reply_ts"], f"First pass: {fast.verdict}", blocks[:50])

    async def post_evidence_only(self, inv_id: int, prefetch: dict, evidence: dict, reason: str,
                                 footer: str) -> str:
        inv = await self.ensure_parent(inv_id)
        blocks = evidence_only_blocks(prefetch, evidence, allowed=self.allowed, reason=reason, footer=footer)
        ts = (await post_blocks(self.slack, inv["slack_channel"], plain_text(blocks, 200), blocks,
                                inv["slack_ts"]))[0]
        telemetry.REPORTS.labels(outcome="evidence_only").inc()
        if inv["reply_ts"] in (None, PENDING):
            await self.store.update_investigation(inv_id, reply_ts=ts)
        return ts

    async def health(self, text: str) -> None:
        try:
            await self.slack.post(self.s.slack_health_channel, sanitize(text, self.allowed))
        except Exception:  # noqa: BLE001 - a health note must never break intake
            log.exception("could not post a health note")
