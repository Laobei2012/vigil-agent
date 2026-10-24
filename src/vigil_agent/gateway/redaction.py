"""Secret and PII masking for tool output, before it reaches the model or Slack.

Secret redaction is always on. PII redaction (emails, IP addresses) is off by
default and turned on per deployment. Each newly found secret format gets a
failing test in tests/fixtures/secrets.txt first.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"

# (name, pattern, group to mask). Group 0 masks the whole match.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)",
            re.S,
        ),
        0,
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"), 0),
    (
        "aws_secret_key",
        re.compile(r"(?i)(aws_?secret_?(?:access_?)?key[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9/+=]{40})"),
        2,
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), 0),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"), 0),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}|\bxapp-[A-Za-z0-9-]{10,}"), 0),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), 0),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("pagerduty_key", re.compile(r"\bu\+[A-Za-z0-9_-]{18,}"), 0),
    ("bearer", re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._~+/=-]{12,})"), 2),
    ("basic_auth", re.compile(r"(?i)(\bbasic\s+)([A-Za-z0-9+/=]{12,})"), 2),
    # scheme://user:password@host. The lookbehind and the bounds keep the time linear. Without
    # them, a long run of letters takes quadratic time, about 9 minutes for 1 MB.
    (
        "url_password",
        re.compile(r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s:/@]{1,128}:)([^\s@/]{1,256})(@)"),
        2,
    ),
    # key=value or key: value for secret-looking keys.
    (
        "kv_secret",
        re.compile(
            r"(?i)((?:^|[\s,{;\"'])(?:[a-z0-9_.-]*(?:password|passwd|pwd|secret|token|"
            r"api[_-]?key|apikey|access[_-]?key|private[_-]?key|credentials?|auth))"
            r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}]{4,})"
        ),
        2,
    ),
]

_PII_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), 0),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), 0),
]

# Keys whose values are masked whole, for example in ConfigMap data. "auth" skips
# "author" and "authority", because commit authors are not secrets.
SECRET_KEY_PATTERN = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
    r"credential|authori[sz]ation|auth(?!or)|dsn|connection[_-]?string|\.pem$|\.key$)"
)


class Redactor:
    def __init__(self, pii: bool = False):
        self.patterns = list(_SECRET_PATTERNS) + (list(_PII_PATTERNS) if pii else [])

    def text(self, value: str) -> str:
        for _name, pattern, group in self.patterns:
            if group == 0:
                value = pattern.sub(MASK, value)
            else:
                value = pattern.sub(lambda m, g=group: _mask_group(m, g), value)
        return value

    def value(self, value: Any) -> Any:
        """Redact strings at any depth of a JSON-like value."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if isinstance(k, str) and isinstance(v, str) and SECRET_KEY_PATTERN.search(k):
                    out[k] = MASK
                else:
                    out[k] = self.value(v)
            return out
        if isinstance(value, list | tuple):
            return [self.value(v) for v in value]
        return value


def _mask_group(match: re.Match[str], group: int) -> str:
    start, end = match.span(group)
    whole_start = match.start(0)
    s = match.group(0)
    return s[: start - whole_start] + MASK + s[end - whole_start :]
