"""Render reports and pre-fetch evidence to Slack Block Kit, within Slack limits.

All model text passes through sanitize first: no mass mentions, no user
mentions, and no links outside the allowlist. Evidence links come from the
orchestrator's own evidence list, never from model text.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from vigil_agent.agent.report import Report

SECTION_LIMIT = 3000
BLOCKS_PER_MESSAGE = 50
SUGGESTED = "suggested, not executed"

_MENTION = re.compile(r"<!(?:here|channel|everyone)(?:\|[^>]*)?>|<!subteam\^[^>]+>|<@[UW][A-Z0-9]+(?:\|[^>]*)?>")
_AT = re.compile(r"(?<![\w`])@(here|channel|everyone)\b", re.I)
_SLACK_LINK = re.compile(r"<(https?://[^|>\s]+)(?:\|([^>]*))?>")
_BARE_URL = re.compile(r"(?<![<|])\bhttps?://[^\s<>|]+")
_ICONS = {"high": ":red_circle:", "medium": ":large_orange_circle:", "low": ":white_circle:"}
_STATUS = {
    "confirmed": "confirmed",
    "ruled_out": "ruled out",
    "replaced": "replaced",
    "new": "new",
    "unchanged": "unchanged",
}


def domain_allowed(url: str, allowed: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


def sanitize(text: str, allowed: list[str]) -> str:
    text = _MENTION.sub("[mention removed]", text)
    text = _AT.sub(lambda m: m.group(1), text)

    def slack_link(m: re.Match) -> str:
        url, label = m.group(1), m.group(2)
        if domain_allowed(url, allowed):
            return m.group(0)
        return f"{label or ''} ({_defang(url)})".strip()

    text = _SLACK_LINK.sub(slack_link, text)
    text = _BARE_URL.sub(lambda m: m.group(0) if domain_allowed(m.group(0), allowed) else _defang(m.group(0)), text)
    return text


def _defang(url: str) -> str:
    return "`" + url.replace("://", "[:]//").replace("`", "") + "`"


def _section(text: str) -> list[dict]:
    """Split text into section blocks of at most SECTION_LIMIT characters."""
    blocks = []
    while text:
        chunk = text[:SECTION_LIMIT]
        if len(text) > SECTION_LIMIT:
            cut = chunk.rfind("\n")
            if cut > SECTION_LIMIT // 2:
                chunk = chunk[:cut]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
        text = text[len(chunk) :].lstrip("\n")
    return blocks


def split_messages(blocks: list[dict]) -> list[list[dict]]:
    return [blocks[i : i + BLOCKS_PER_MESSAGE] for i in range(0, len(blocks), BLOCKS_PER_MESSAGE)] or [[]]


def parent_blocks(
    *,
    severity: str,
    service: str,
    cluster: str,
    title: str,
    pd_url: str | None,
    status: str,
    verdict: str | None,
    allowed: list[str],
) -> tuple[str, list[dict]]:
    sev = {"high": ":rotating_light:", "low": ":warning:"}.get(severity, ":rotating_light:")
    head = f"{sev} *{sanitize(title, allowed)}*"
    fields = f"*Service:* {service}   *Cluster:* {cluster}   *Status:* {status}"
    if pd_url:
        fields += f"   <{pd_url}|incident>"
    line = f":mag: {sanitize(verdict, allowed)}" if verdict else ":hourglass_flowing_sand: investigating..."
    text = f"{title} [{status}]"
    return text, [{"type": "section", "text": {"type": "mrkdwn", "text": f"{head}\n{fields}\n{line}"[:SECTION_LIMIT]}}]


def report_blocks(
    report: Report,
    evidence: dict[str, dict],
    *,
    allowed: list[str],
    footer: str,
    label: str,
    investigation_id: int,
    buttons: bool = True,
) -> list[dict]:
    s = lambda t: sanitize(t, allowed)  # noqa: E731
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": label[:150]}}]
    blocks += _section(
        f"*Summary:* {s(report.summary)}\n*Impact:* {s(report.impact)}\n*Confidence:* {report.confidence}"
    )
    if report.timeline:
        blocks += _section("*Timeline*\n" + "\n".join(f"• {s(t)}" for t in report.timeline))
    lines = []
    for i, h in enumerate(report.hypotheses, 1):
        cites = [e for e in h.evidence_ids if e in evidence]
        support = ", ".join(_cite(e, evidence[e]) for e in cites) if cites else "_unsupported: no evidence cited_"
        status = f" [{_STATUS[h.status]}]" if report.mode == "deep" and h.status != "new" else ""
        note = f" {s(h.note)}" if h.note else ""
        lines.append(
            f"{_ICONS[h.confidence]} *{i}. {s(h.cause)}* ({h.category}, {h.confidence}){status}{note}\n"
            f"    evidence: {support}"
        )
    blocks += _section("*Likely cause*\n" + ("\n".join(lines) or "unknown"))
    mit = []
    for i, m in enumerate(report.mitigations, 1):
        cmd = f"\n    `{m.command.replace('`', '')}`" if m.command else ""
        ref = f"\n    runbook: {s(m.runbook_ref)}" if m.runbook_ref else ""
        mit.append(f"*{i}. {s(m.action)}* (risk {m.risk}): {s(m.expected_effect)}{cmd}{ref}")
    blocks += _section(f"*Suggested mitigations* ({SUGGESTED})\n" + ("\n".join(mit) or "none"))
    if evidence:
        blocks += _section(
            "*Evidence*\n" + "\n".join(f"• {_cite(k, v)} {v.get('title', '')[:150]}" for k, v in evidence.items())
        )
    if report.gaps:
        blocks += _section("*Gaps*\n" + "\n".join(f"• {s(g)}" for g in report.gaps))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer[:3000]}]})
    if buttons:
        blocks.append(feedback_actions(investigation_id))
    return blocks


def _cite(eid: str, ev: dict) -> str:
    url = ev.get("url")
    return f"<{url}|{eid}>" if url else eid


def feedback_actions(investigation_id: int) -> dict:
    return {
        "type": "actions",
        "block_id": f"vigil_feedback_{investigation_id}",
        "elements": [
            {
                "type": "button",
                "action_id": "vigil_helpful",
                "text": {"type": "plain_text", "text": "Helpful"},
                "value": str(investigation_id),
            },
            {
                "type": "button",
                "action_id": "vigil_not_helpful",
                "text": {"type": "plain_text", "text": "Not helpful"},
                "value": str(investigation_id),
            },
            {
                "type": "button",
                "action_id": "vigil_reinvestigate",
                "text": {"type": "plain_text", "text": "Re-investigate"},
                "value": str(investigation_id),
            },
        ],
    }


def _similar(x: dict) -> str:
    """One line for a past incident: a PagerDuty incident or an earlier investigation."""
    if x.get("title"):
        return f"{x['title']} ({x.get('created_at', '')[:10]}, {x.get('resolve_reason') or 'resolved'})"
    when = str(x.get("at", ""))[:16]
    return f"investigation {x.get('investigation')} on {when}: {x.get('verdict') or 'evidence only, no verdict'}"


def _num(v) -> str:
    try:
        return f"{float(v):.4g}"
    except (TypeError, ValueError):
        return "?"


def evidence_only_blocks(
    prefetch: dict,
    evidence: dict[str, dict],
    *,
    allowed: list[str],
    reason: str,
    footer: str,
    alerts: list[dict] | None = None,
) -> list[dict]:
    """The degraded-mode reply: the deterministic pre-fetch, without model analysis."""
    s = lambda t: sanitize(str(t), allowed)  # noqa: E731
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": "Evidence (no model analysis)"}}]
    blocks += _section(f"_{s(reason)}_")
    lines = []
    for a in (alerts or [])[:5]:
        labels = a.get("labels", {})
        keys = ("namespace", "pod", "container", "job_name", "node", "pvc", "instance")
        where = ", ".join(f"{k}={labels[k]}" for k in keys if labels.get(k))
        text = a.get("annotations", {}).get("description") or a.get("annotations", {}).get("summary", "")
        lines.append(f"• `{labels.get('alertname', '?')}` {where}: {s(text)[:400]}")
    if lines:
        blocks += _section("*Alert*\n" + "\n".join(lines))
    rule = prefetch.get("alert_rule") or {}
    if rule:
        blocks += _section(
            f"*Alert rule* `{rule.get('name')}` (for {rule.get('duration', 0)}s): `{(rule.get('query') or '')[:500]}`"
        )
    metrics = []
    for m in prefetch.get("metrics", [])[:4]:
        series = m.get("series", [])
        metrics.append(
            f"• {_cite(m['evidence_id'], evidence.get(m['evidence_id'], {}))} `{m['query'][:150]}`: "
            f"{len(series)} series"
        )
        for x in series[:3]:
            label = ",".join(f"{k}={v}" for k, v in x.get("metric", {}).items() if k != "__name__")[:120]
            last = (x.get("last") or [None, None])[1]
            metrics.append(f"    {label or 'value'}: last {_num(last)}, max {_num(x.get('max'))} (1 h)")
    if metrics:
        blocks += _section("*Metrics*\n" + "\n".join(metrics))
    build = prefetch.get("jenkins") or {}
    if build:
        cite = _cite(build["evidence_id"], evidence.get(build["evidence_id"], {}))
        errs = "\n".join(
            f"• line {e['line']}: `{s(e['text'])[:240].replace('`', '')}`" for e in build.get("error_lines", [])[-8:]
        )
        blocks += _section(
            f"*Jenkins build* {build.get('job')} #{build.get('build')} {build.get('result')} on "
            f"{build.get('agent')} ({cite})\n" + (errs or "no error lines found")
        )
    logs = prefetch.get("logs") or {}
    if logs.get("top_patterns"):
        top = "\n".join(f"• {p['count']}x `{s(p['example'])[:240].replace('`', '')}`" for p in logs["top_patterns"][:5])
        cite = _cite(logs["evidence_id"], evidence.get(logs["evidence_id"], {}))
        blocks += _section(f"*Top log lines* ({logs.get('hits', 0)} matching lines, {cite})\n{top}")
    changes = prefetch.get("timeline", {}).get("entries", [])[:15]
    if changes:
        blocks += _section(
            "*Recent changes (2 h)*\n"
            + "\n".join(
                f"• {c['time']} {c['source']}: {sanitize(c.get('summary', ''), allowed)[:200]}" for c in changes
            )
        )
    events = prefetch.get("events", [])[:10]
    if events:
        blocks += _section(
            "*Kubernetes warnings*\n"
            + "\n".join(
                f"• {e['time']} {e['reason']} {e['object']}: {sanitize(e['message'], allowed)[:200]}" for e in events
            )
        )
    similar = prefetch.get("similar", [])
    if similar:
        blocks += _section(
            "*Similar past incidents*\n" + "\n".join(f"• {sanitize(_similar(x), allowed)[:200]}" for x in similar)
        )
    if prefetch.get("runbook"):
        blocks += _section(f"*Runbook:* {sanitize(prefetch['runbook'], allowed)}")
    if evidence:
        blocks += _section(
            "*Evidence*\n" + "\n".join(f"• {_cite(k, v)} {v.get('title', '')[:150]}" for k, v in evidence.items())
        )
    if prefetch.get("gaps"):
        blocks += _section("*Gaps*\n" + "\n".join(f"• {sanitize(g, allowed)}" for g in prefetch["gaps"]))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer[:3000]}]})
    return blocks


def plain_text(blocks: list[dict], limit: int = 3000) -> str:
    """Fallback text for notifications."""
    parts = []
    for b in blocks:
        t = b.get("text")
        if isinstance(t, dict):
            parts.append(t.get("text", ""))
    return "\n".join(parts)[:limit]
