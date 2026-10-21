"""Recorded-model stand-ins: no Bedrock call in any test."""

from __future__ import annotations

import itertools

from anthropic.types import Message

_ids = itertools.count(1)


def msg(*blocks: dict, stop_reason: str | None = None) -> Message:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    return Message.model_validate({
        "id": f"msg_{next(_ids)}", "type": "message", "role": "assistant", "model": "fake",
        "content": list(blocks), "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0},
    })


def tool_use(name: str, args: dict) -> dict:
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def thinking() -> dict:
    return {"type": "thinking", "thinking": "", "signature": "sig"}


def report_args(mode: str = "fast", **kw) -> dict:
    base = {
        "mode": mode, "verdict": "Job failed after a chart bump", "summary": "The job fails.",
        "impact": "No cost data.", "timeline": ["10:00 chart bump"], "confidence": "medium",
        "hypotheses": [{"cause": "chart bump", "category": "bad_deploy", "confidence": "high",
                        "evidence_ids": ["E1"]}],
        "mitigations": [{"action": "revert the PR", "risk": "low", "expected_effect": "job passes",
                         "command": "git revert abc"}],
        "gaps": [],
    }
    base.update(kw)
    return base


class FakeModel:
    """Returns scripted responses in order. Each item is a Message or a callable(messages) -> Message."""

    model_id = "fake-opus"

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []

    async def create(self, *, system, tools, messages, max_tokens):
        self.requests.append({"system": system, "tools": tools, "messages": [dict(m) for m in messages]})
        if not self.script:
            raise AssertionError("FakeModel script is exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(messages) if callable(item) else item
