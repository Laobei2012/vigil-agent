"""The submit_report tool and its schema. Both passes end with one call to it."""

from __future__ import annotations

import re
import shlex
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Subcommands that only read. A command whose every part is one of these changes nothing.
_READ_VERBS = {
    "kubectl": {"get", "describe", "logs", "top", "events", "explain", "api-resources", "version"},
    "flux": {"get", "logs", "tree", "stats", "check", "version"},
    "git": {"log", "show", "diff", "status", "blame", "grep"},
    "helm": {"list", "ls", "status", "history", "get", "show", "template"},
}
_READ_SUBVERBS = {("kubectl", "rollout"): {"status", "history"}, ("kubectl", "auth"): {"can-i"}}
_FILTERS = {"grep", "egrep", "head", "tail", "wc", "jq", "sort", "uniq", "cut", "less"}
# Flags that take the next word as their value.
_VALUE_FLAGS = {"-n", "--namespace", "--context", "-l", "--selector", "-C", "-c", "--container", "-o", "--output"}


def _part_reads_only(part: str) -> bool:
    try:
        words = shlex.split(part)
    except ValueError:
        return False
    if not words:
        return True
    tool = words[0]
    if tool in _FILTERS:
        return True
    if tool not in _READ_VERBS:
        return False
    rest, i = [], 1
    while i < len(words):
        w = words[i]
        if w in _VALUE_FLAGS:
            i += 2
            continue
        if not w.startswith("-"):
            rest.append(w)
        i += 1
    if not rest:
        return False
    if (tool, rest[0]) in _READ_SUBVERBS:
        return len(rest) > 1 and rest[1] in _READ_SUBVERBS[(tool, rest[0])]
    return rest[0] in _READ_VERBS[tool]


def read_only_command(command: str | None) -> bool:
    """True when every part of a shell command only reads. Unknown or empty commands are not read-only."""
    if not command or not command.strip():
        return False
    lines = [line.split(" #")[0] for line in command.splitlines()]
    parts = [p.strip() for p in re.split(r"&&|\|\||;|\|", "\n".join(lines).replace("\n", ";"))]
    parts = [p for p in parts if p]
    return bool(parts) and all(_part_reads_only(p) for p in parts)


CATEGORIES = [
    "bad_deploy",
    "config_change",
    "resource_exhaustion",
    "dependency_failure",
    "infrastructure",
    "capacity",
    "certificate",
    "secret_or_credential",
    "external",
    "flapping_or_noise",
    "unknown",
]


class Hypothesis(BaseModel):
    cause: str
    category: str = "unknown"
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str] = Field(default_factory=list)
    # Deep pass only: how the hypothesis compares with the fast pass.
    status: Literal["new", "confirmed", "ruled_out", "replaced", "unchanged"] = "new"
    note: str | None = None

    @field_validator("category")
    @classmethod
    def _category(cls, v: str) -> str:
        return v if v in CATEGORIES else "unknown"


class Mitigation(BaseModel):
    action: str
    risk: Literal["low", "medium", "high"]
    expected_effect: str
    command: str | None = None
    runbook_ref: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Report(BaseModel):
    mode: Literal["fast", "deep"]
    verdict: str = Field(description="One line for the parent message.")
    summary: str
    impact: str
    timeline: list[str] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(max_length=3)
    mitigations: list[Mitigation] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"]

    @model_validator(mode="after")
    def _act_before_read(self) -> Report:
        """A step that only reads is not a mitigation, so it never ranks above one that acts.

        The responder reads the first mitigation first. The model sometimes puts an
        inventory step first; the stable sort keeps the model's order otherwise.
        """
        self.mitigations.sort(key=lambda m: read_only_command(m.command))
        return self


SUBMIT_REPORT_TOOL = {
    "name": "submit_report",
    "description": (
        "Submit the triage report. Call it exactly once, as your final action. Cite evidence by the "
        "evidence IDs (E1, E2, ...) shown with each tool result; never invent IDs or URLs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["fast", "deep"]},
            "verdict": {"type": "string", "description": "One line: the most likely cause and the next step."},
            "summary": {"type": "string", "description": "One or two sentences: what is happening, who is affected."},
            "impact": {
                "type": "string",
                "description": "Affected service and endpoints, error rate or latency "
                "against baseline, start time. Say unknown where unknown.",
            },
            "timeline": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered events: alert "
                "start, deploys, config changes, related alerts, Kubernetes events, each with time.",
            },
            "hypotheses": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "cause": {"type": "string"},
                        "category": {"type": "string", "enum": CATEGORIES},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "status": {
                            "type": "string",
                            "enum": ["new", "confirmed", "ruled_out", "replaced", "unchanged"],
                            "description": "Deep pass: compare with the first-pass hypothesis.",
                        },
                        "note": {"type": "string"},
                    },
                    "required": ["cause", "category", "confidence", "evidence_ids"],
                },
            },
            "mitigations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                        "expected_effect": {"type": "string"},
                        "command": {"type": "string", "description": "Exact command or runbook step for a human."},
                        "runbook_ref": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["action", "risk", "expected_effect"],
                },
                "description": "Ranked, lowest risk first among equally effective options.",
            },
            "gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What you could not check or found inconclusive.",
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["mode", "verdict", "summary", "impact", "hypotheses", "mitigations", "gaps", "confidence"],
    },
}
