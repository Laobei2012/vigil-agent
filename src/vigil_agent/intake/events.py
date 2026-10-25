"""Intake events: PagerDuty V3 webhooks and Alertmanager webhooks, in one shape."""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from vigil_agent.gateway.redaction import Redactor

EventType = Literal["incident.triggered", "incident.acknowledged", "incident.resolved", "incident.reopened"]
EVENT_TYPES = ("incident.triggered", "incident.acknowledged", "incident.resolved", "incident.reopened")

# Caps for one event, so that a large group or a long annotation cannot fill the prompt.
# The PagerDuty client also reads at most 100 alerts.
MAX_ALERTS = 100
MAX_VALUE_CHARS = 2000

# Alert text reaches the prompt and Slack without passing the gateway, so intake redacts it too.
_REDACTOR = Redactor()


class Alert(BaseModel):
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    status: str = "firing"
    starts_at: str | None = None
    generator_url: str | None = None

    @field_validator("labels", "annotations")
    @classmethod
    def _cap_values(cls, values: dict[str, str]) -> dict[str, str]:
        return _REDACTOR.value({k: v[:MAX_VALUE_CHARS] for k, v in values.items()})


class IntakeEvent(BaseModel):
    source: Literal["pagerduty", "alertmanager", "early_start"]
    event_id: str
    event_type: EventType
    incident_id: str
    service_id: str | None = None
    title: str
    urgency: str = "high"
    html_url: str | None = None
    occurred_at: datetime | None = None
    alerts: list[Alert] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _cap_title(cls, title: str) -> str:
        return _REDACTOR.text(title[:MAX_VALUE_CHARS])

    def label(self, name: str) -> str | None:
        for a in self.alerts:
            if a.labels.get(name):
                return a.labels[name]
        return None


def verify_pd_signature(body: bytes, header: str | None, secrets: list[str]) -> bool:
    """X-PagerDuty-Signature carries one or more v1=<hex HMAC-SHA256> values."""
    if not header or not secrets:
        return False
    sigs = [p.strip()[3:] for p in header.split(",") if p.strip().startswith("v1=")]
    for secret in secrets:
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if any(hmac.compare_digest(expected, s) for s in sigs):
            return True
    return False


def parse_pd_webhook(body: dict) -> IntakeEvent | None:
    """Return None for event types outside F1 or for non-incident resources."""
    event = body.get("event") or {}
    if event.get("event_type") not in EVENT_TYPES or event.get("resource_type", "incident") != "incident":
        return None
    data = event.get("data") or {}
    return IntakeEvent(
        source="pagerduty",
        event_id=event["id"],
        event_type=event["event_type"],
        incident_id=data["id"],
        service_id=(data.get("service") or {}).get("id"),
        title=data.get("title") or data.get("summary") or "",
        urgency=data.get("urgency") or "high",
        html_url=data.get("html_url"),
        occurred_at=event.get("occurred_at"),
    )


_LABEL_LINE = re.compile(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _firing_lines(firing) -> list[str]:
    """The "firing" detail as text lines.

    PagerDuty parses Alertmanager's text into {"Labels": [...], "Annotations": [...]},
    or a list of these for a group. An item is "key = value", or a one-key dict when
    the value had ": " in it.
    """
    if isinstance(firing, str):
        return firing.splitlines()
    lines: list[str] = []
    for block in firing if isinstance(firing, list) else [firing]:
        if not isinstance(block, dict):
            continue
        for section in ("Labels", "Annotations"):
            lines.append(f"{section}:")
            for item in block.get(section) or []:
                if isinstance(item, dict):
                    lines += [f" - {k}: {v}" for k, v in item.items()]
                else:
                    lines.append(f" - {item}")
        lines.append("Source:")
    return lines


def pd_alert_to_alert(pd_alert: dict) -> Alert:
    """Read the alert labels from a PagerDuty alert's custom details.

    Preferred: Alertmanager's pagerduty_configs.details carries cluster,
    namespace, and alertname keys. Fallback: the default "firing" detail text,
    which lists "Labels:" lines like " - alertname = X".
    """
    body = pd_alert.get("body") or {}
    details = body.get("details") or (body.get("cef_details") or {}).get("details") or {}
    if isinstance(details, str):
        details = {"firing": details}
    labels: dict[str, str] = {}
    for key in ("cluster", "namespace", "alertname", "severity", "pod", "job", "service", "app"):
        if isinstance(details.get(key), str) and details[key]:
            labels[key] = details[key]
    annotations: dict[str, str] = {}
    section = None
    for line in _firing_lines(details.get("firing", "")):
        stripped = line.strip()
        if stripped in ("Labels:", "Annotations:"):
            section = stripped
            continue
        m = _LABEL_LINE.match(line)
        if m and section == "Labels:":
            labels.setdefault(m.group(1), m.group(2))
        elif m and section == "Annotations:":
            annotations.setdefault(m.group(1), m.group(2))
        elif stripped.startswith("Source:"):
            section = None
    for key in ("summary", "description", "runbook_url"):
        if isinstance(details.get(key), str):
            annotations.setdefault(key, details[key])
    return Alert(labels=labels, annotations=annotations, status=pd_alert.get("status", "triggered"))


def parse_alertmanager(body: dict) -> IntakeEvent:
    """One Alertmanager group notification becomes one intake event.

    The incident ID is stable per group key. The event ID changes when the
    status or the set of alerts changes, so a repeat notification dedupes.
    """
    group_key = body.get("groupKey", "")
    incident_id = "am-" + hashlib.sha256(group_key.encode()).hexdigest()[:16]
    status = body.get("status", "firing")
    alerts = [
        Alert(
            labels=a.get("labels", {}),
            annotations=a.get("annotations", {}),
            status=a.get("status", status),
            starts_at=a.get("startsAt"),
            generator_url=a.get("generatorURL"),
        )
        for a in body.get("alerts", [])[:MAX_ALERTS]
    ]
    fingerprint = ",".join(
        sorted(f"{a.get('fingerprint')}:{a.get('status')}:{a.get('startsAt')}" for a in body.get("alerts", []))
    )
    event_id = "am-" + hashlib.sha256(f"{group_key}|{status}|{fingerprint}".encode()).hexdigest()[:24]
    common = body.get("commonLabels", {})
    alertname = common.get("alertname") or (alerts[0].labels.get("alertname") if alerts else "alert")
    summary = body.get("commonAnnotations", {}).get("summary") or (
        alerts[0].annotations.get("summary") if alerts else ""
    )
    severity = common.get("severity", "warning")
    return IntakeEvent(
        source="alertmanager",
        event_id=event_id,
        event_type="incident.resolved" if status == "resolved" else "incident.triggered",
        incident_id=incident_id,
        title=f"{alertname}: {summary}" if summary else alertname,
        urgency="high" if severity == "critical" else "low",
        html_url=body.get("externalURL"),
        alerts=alerts,
    )
