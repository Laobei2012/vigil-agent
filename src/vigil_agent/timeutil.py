"""Time parsing for tool arguments: RFC 3339, "now", or "now-1h"."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_DURATION = re.compile(r"^(\d+)(s|m|h|d|w)$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_duration(value: str) -> timedelta:
    m = _DURATION.match(value.strip())
    if not m:
        raise ValueError(f"invalid duration {value!r}, use for example 30m, 2h, 7d")
    return timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2)])


def parse_time(value: str | None, now: datetime | None = None) -> datetime:
    now = now or utcnow()
    if value is None or value == "now":
        return now
    value = value.strip()
    if value.startswith("now-"):
        return now - parse_duration(value[4:])
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid time {value!r}, use RFC 3339 or now-<duration>") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
