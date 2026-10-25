"""Render reports and pre-fetch evidence to Slack Block Kit, within Slack limits.

Every interpolated value is untrusted: model text, tool arguments, alert
labels, and backend fields. Model text passes through sanitize (no mentions,
no links outside the allowlist). Every other value passes through escape,
which turns "&", "<", and ">" into entities, so that Slack cannot read it as
a mention or a link. Links come only from the link helper.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlparse

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


def escape(text) -> str:
    """Slack's required escaping for untrusted text: & < > become entities."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def safe_url(url: str | None) -> str | None:
    """The URL ready for <url|label>, or None when it is not a plain https URL."""
    if not isinstance(url, str) or not url.startswith("https://") or "\\" in url:
        return None
    parsed = urlparse(url)
    # User information hides the real host: https://evil.example\@allowed.example or https://a@b.
    if "@" in parsed.netloc or not parsed.hostname:
        return None
    return quote(url, safe="/:?=&%#+,;.-_~!$'()*[]")


def domain_allowed(url: str, allowed: list[str]) -> bool:
    if not safe_url(url):
        return False
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


def link(url: str | None, label: str, allowed: list[str] | None = None) -> str:
    """A Slack link with an escaped label. Without a safe URL, the escaped label alone.

    With allowed, the host must be in the allowlist too. Evidence URLs skip that test,
    because the gateway builds them from its own configuration.
    """
    safe = safe_url(url)
    if not safe or (allowed is not None and not domain_allowed(url, allowed)):
        return escape(label)
    return f"<{safe}|{escape(label)}>"


def sanitize(text: str, allowed: list[str]) -> str:
    """Model text: no mentions, links only to allowed hosts, everything else escaped."""
    text = _MENTION.sub("[mention removed]", str(text))
    text = _AT.sub(lambda m: m.group(1), text)
    out, last = [], 0
    for m in _SLACK_LINK.finditer(text):
        out.append(_plain(text[last : m.start()], allowed))
        url, label = m.group(1), m.group(2)
        if domain_allowed(url, allowed):
            out.append(link(url, label or url, allowed))
        else:
            out.append(f"{escape(label or '')} ({_defang(url)})".strip())
        last = m.end()
    out.append(_plain(text[last:], allowed))
    return "".join(out)


def _plain(text: str, allowed: list[str]) -> str:
    """Text outside a Slack link: bare URLs to other hosts are defanged, then all is escaped."""
    out, last = [], 0
    for m in _BARE_URL.finditer(text):
        out.append(escape(text[last : m.start()]))
        url = m.group(0)
        out.append(escape(url) if domain_allowed(url, allowed) else _defang(url))
        last = m.end()
    out.append(escape(text[last:]))
    return "".join(out)


def _defang(url: str) -> str:
    return "`" + escape(url.replace("://", "[:]//").replace("`", "")) + "`"


def code_block(text: str) -> str:
    """A fenced block. Fence characters are removed, so the text cannot close the block."""
    return "```\n" + escape(str(text).replace("`", "'")) + "\n```"


def code(text) -> str:
    """Inline code on one line. A newline would end the span, so newlines become spaces."""
    return "`" + escape(" ".join(str(text).replace("`", "'").split())) + "`"


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
    fields = f"*Service:* {escape(service)}   *Cluster:* {escape(cluster)}   *Status:* {escape(status)}"
    if pd_url and domain_allowed(pd_url, allowed):
        fields += f"   {link(pd_url, 'incident', allowed)}"
    line = f":mag: {sanitize(verdict, allowed)}" if verdict else ":hourglass_flowing_sand: investigating..."
    text = escape(f"{title} [{status}]")
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
        f"*Summary:* {s(report.summary)}\n*Impact:* {s(report.impact)}\n*Confidence:* {escape(report.confidence)}"
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
            f"{_ICONS[h.confidence]} *{i}. {s(h.cause)}* ({escape(h.category)}, {h.confidence}){status}{note}\n"
            f"    evidence: {support}"
        )
    blocks += _section("*Likely cause*\n" + ("\n".join(lines) or "unknown"))
    mit = []
    for i, m in enumerate(report.mitigations, 1):
        cmd = f"\n{code_block(m.command)}" if m.command else ""
        ref = f"\n    runbook: {s(m.runbook_ref)}" if m.runbook_ref else ""
        mit.append(f"*{i}. {s(m.action)}* (risk {escape(m.risk)}): {s(m.expected_effect)}{cmd}{ref}")
    blocks += _section(f"*Suggested mitigations* ({SUGGESTED})\n" + ("\n".join(mit) or "none"))
    if evidence:
        blocks += _evidence_section(evidence)
    if report.gaps:
        blocks += _section("*Gaps*\n" + "\n".join(f"• {s(g)}" for g in report.gaps))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer[:3000]}]})
    if buttons:
        blocks.append(feedback_actions(investigation_id))
    return blocks


def _cite(eid: str, ev: dict) -> str:
    return link(ev.get("url"), eid)


def _evidence_section(evidence: dict[str, dict]) -> list[dict]:
    # A title holds tool arguments, which the model or an alert chose.
    return _section(
        "*Evidence*\n"
        + "\n".join(f"• {_cite(k, v)} {escape(str(v.get('title', ''))[:150])}" for k, v in evidence.items())
    )


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
        where = escape(", ".join(f"{k}={labels[k]}" for k in keys if labels.get(k)))
        text = a.get("annotations", {}).get("description") or a.get("annotations", {}).get("summary", "")
        lines.append(f"• {code(labels.get('alertname', '?'))} {where}: {s(str(text)[:400])}")
    if lines:
        blocks += _section("*Alert*\n" + "\n".join(lines))
    rule = prefetch.get("alert_rule") or {}
    if rule:
        blocks += _section(
            f"*Alert rule* {code(rule.get('name'))} (for {_num(rule.get('duration', 0))}s): "
            f"{code((rule.get('query') or '')[:500])}"
        )
    metrics = []
    for m in prefetch.get("metrics", [])[:4]:
        series = m.get("series", [])
        metrics.append(
            f"• {_cite(m['evidence_id'], evidence.get(m['evidence_id'], {}))} {code(m['query'][:150])}: "
            f"{len(series)} series"
        )
        for x in series[:3]:
            label = ",".join(f"{k}={v}" for k, v in x.get("metric", {}).items() if k != "__name__")[:120]
            last = (x.get("last") or [None, None])[1]
            metrics.append(f"    {escape(label or 'value')}: last {_num(last)}, max {_num(x.get('max'))} (1 h)")
    if metrics:
        blocks += _section("*Metrics*\n" + "\n".join(metrics))
    build = prefetch.get("jenkins") or {}
    if build:
        cite = _cite(build["evidence_id"], evidence.get(build["evidence_id"], {}))
        errs = "\n".join(
            f"• line {escape(e['line'])}: {code(str(e['text'])[:240])}" for e in build.get("error_lines", [])[-8:]
        )
        blocks += _section(
            f"*Jenkins build* {escape(build.get('job'))} #{escape(build.get('build'))} {escape(build.get('result'))} "
            f"on {escape(build.get('agent'))} ({cite})\n" + (errs or "no error lines found")
        )
    logs = prefetch.get("logs") or {}
    if logs.get("top_patterns"):
        top = "\n".join(f"• {escape(p['count'])}x {code(str(p['example'])[:240])}" for p in logs["top_patterns"][:5])
        cite = _cite(logs["evidence_id"], evidence.get(logs["evidence_id"], {}))
        blocks += _section(f"*Top log lines* ({escape(logs.get('hits', 0))} matching lines, {cite})\n{top}")
    changes = prefetch.get("timeline", {}).get("entries", [])[:15]
    if changes:
        blocks += _section(
            "*Recent changes (2 h)*\n"
            + "\n".join(
                f"• {escape(c['time'])} {escape(c['source'])}: {sanitize(str(c.get('summary', ''))[:200], allowed)}"
                for c in changes
            )
        )
    events = prefetch.get("events", [])[:10]
    if events:
        blocks += _section(
            "*Kubernetes warnings*\n"
            + "\n".join(
                f"• {escape(e['time'])} {escape(e['reason'])} {escape(e['object'])}: "
                f"{sanitize(str(e['message'])[:200], allowed)}"
                for e in events
            )
        )
    similar = prefetch.get("similar", [])
    if similar:
        blocks += _section(
            "*Similar past incidents*\n" + "\n".join(f"• {sanitize(_similar(x)[:200], allowed)}" for x in similar)
        )
    if prefetch.get("runbook"):
        blocks += _section(f"*Runbook:* {sanitize(prefetch['runbook'], allowed)}")
    if evidence:
        blocks += _evidence_section(evidence)
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
