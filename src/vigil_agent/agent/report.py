"""The submit_report tool and its schema. Both passes end with one call to it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

CATEGORIES = ["bad_deploy", "config_change", "resource_exhaustion", "dependency_failure", "infrastructure",
              "capacity", "certificate", "secret_or_credential", "external", "flapping_or_noise", "unknown"]


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
            "summary": {"type": "string", "description": "One or two sentences: what is happening, who is "
                        "affected."},
            "impact": {"type": "string", "description": "Affected service and endpoints, error rate or latency "
                       "against baseline, start time. Say unknown where unknown."},
            "timeline": {"type": "array", "items": {"type": "string"}, "description": "Ordered events: alert "
                         "start, deploys, config changes, related alerts, Kubernetes events, each with time."},
            "hypotheses": {
                "type": "array", "maxItems": 3,
                "items": {"type": "object", "properties": {
                    "cause": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string", "enum": ["new", "confirmed", "ruled_out", "replaced",
                                                          "unchanged"],
                               "description": "Deep pass: compare with the first-pass hypothesis."},
                    "note": {"type": "string"}},
                    "required": ["cause", "category", "confidence", "evidence_ids"]}},
            "mitigations": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "action": {"type": "string"},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                    "expected_effect": {"type": "string"},
                    "command": {"type": "string", "description": "Exact command or runbook step for a human."},
                    "runbook_ref": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}}},
                    "required": ["action", "risk", "expected_effect"]},
                "description": "Ranked, lowest risk first among equally effective options."},
            "gaps": {"type": "array", "items": {"type": "string"},
                     "description": "What you could not check or found inconclusive."},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["mode", "verdict", "summary", "impact", "hypotheses", "mitigations", "gaps", "confidence"],
    },
}
