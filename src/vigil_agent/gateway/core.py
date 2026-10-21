"""Tool registry, allowlist, rate limits, shaping, and audit for the tool gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

import yaml

from vigil_agent import telemetry
from vigil_agent.gateway.redaction import Redactor

log = logging.getLogger("vigil.gateway.audit")

MAX_RESULT_CHARS = 60_000


class ToolError(Exception):
    """A tool refused the call or its backend failed. The message goes to the model."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    evidence_url: str | None = None
    truncated: bool = False
    error: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "data": self.data,
            "evidence_url": self.evidence_url,
            "truncated": self.truncated,
            "error": self.error,
            "error_code": self.error_code,
        }


@dataclass
class ToolOutput:
    """What a tool function returns before the gateway shapes and redacts it."""

    data: Any
    evidence_url: str | None = None
    truncated: bool = False


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    backend: str
    fn: Callable[..., Awaitable[ToolOutput]]
    timeout: float | None = None

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def load_allowlist() -> frozenset[str]:
    """The allowlist is a file in the package, so it is baked into the signed image."""
    text = resources.files("vigil_agent.gateway").joinpath("allowlist.yaml").read_text()
    return frozenset(yaml.safe_load(text)["tools"])


class TokenBucket:
    def __init__(self, rate_per_minute: float, burst: int):
        self.rate = rate_per_minute / 60.0
        self.capacity = burst
        self.tokens = float(burst)
        self.updated = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


@dataclass
class Registry:
    allowlist: frozenset[str]
    redactor: Redactor = field(default_factory=Redactor)
    default_timeout: float = 30.0
    rate_limits: dict[str, TokenBucket] = field(default_factory=dict)
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name not in self.allowlist:
            raise ValueError(f"tool {tool.name} is not in the allowlist")
        self.tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        return [t.schema() for t in sorted(self.tools.values(), key=lambda t: t.name)]

    async def call(self, name: str, args: dict, incident_id: str | None) -> ToolResult:
        started = time.monotonic()
        result = await self._call(name, args)
        payload = json.dumps(result.to_dict(), sort_keys=True, default=str)
        status = "ok" if result.ok else (result.error_code or "error")
        backend = self.tools[name].backend if name in self.tools else "none"
        latency = time.monotonic() - started
        telemetry.TOOL_CALLS.labels(tool=name, status=status).inc()
        telemetry.TOOL_LATENCY.labels(tool=name).observe(latency)
        log.info(
            json.dumps(
                {
                    "audit": "tool_call",
                    "tool": name,
                    "backend": backend,
                    "args": self.redactor.value(args),
                    "incident_id": incident_id,
                    "status": status,
                    "latency_ms": round(latency * 1000),
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload.encode()).hexdigest(),
                },
                default=str,
            )
        )
        return result

    async def _call(self, name: str, args: dict) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None or name not in self.allowlist:
            return ToolResult(False, error=f"unknown tool {name}", error_code="unknown_tool")
        problem = validate_args(tool.input_schema, args)
        if problem:
            return ToolResult(False, error=problem, error_code="invalid_arguments")
        bucket = self.rate_limits.get(tool.backend)
        if bucket and not bucket.take():
            return ToolResult(
                False,
                error=f"rate limit for backend {tool.backend}, try later",
                error_code="rate_limited",
            )
        try:
            out = await asyncio.wait_for(tool.fn(**args), tool.timeout or self.default_timeout)
        except TimeoutError:
            return ToolResult(False, error="tool call timed out", error_code="timeout")
        except ToolError as exc:
            return ToolResult(False, error=self.redactor.text(str(exc)), error_code=exc.code)
        except Exception as exc:  # noqa: BLE001 - a backend failure is a result, not a crash
            logging.getLogger("vigil.gateway").exception("tool %s failed", name)
            return ToolResult(
                False,
                error=self.redactor.text(f"{type(exc).__name__}: {exc}")[:500],
                error_code="backend_error",
            )
        data, truncated = shape(self.redactor.value(out.data))
        return ToolResult(True, data, out.evidence_url, out.truncated or truncated)


def shape(data: Any, limit: int = MAX_RESULT_CHARS) -> tuple[Any, bool]:
    """Cap the serialized size of a result. Lists are cut first, then long strings."""
    text = json.dumps(data, default=str)
    if len(text) <= limit:
        return data, False
    if isinstance(data, dict):
        out = dict(data)
        for key, value in sorted(out.items(), key=lambda kv: -len(json.dumps(kv[1], default=str))):
            if isinstance(value, list) and len(value) > 1:
                while len(value) > 1 and len(json.dumps(out, default=str)) > limit:
                    value = value[: max(1, len(value) // 2)]
                    out[key] = value
            elif isinstance(value, str) and len(json.dumps(out, default=str)) > limit:
                out[key] = value[: max(200, limit // 2)] + " ...[truncated]"
            if len(json.dumps(out, default=str)) <= limit:
                break
        if len(json.dumps(out, default=str)) <= limit:
            return out, True
    if isinstance(data, list):
        out_list = list(data)
        while len(out_list) > 1 and len(json.dumps(out_list, default=str)) > limit:
            out_list = out_list[: len(out_list) // 2]
        if len(json.dumps(out_list, default=str)) <= limit:
            return out_list, True
    return {"text": text[:limit] + " ...[truncated]"}, True


_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_args(schema: dict, args: Any) -> str | None:
    """A small JSON-schema check: required keys, unknown keys, types, enums, bounds."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            return f"missing required argument {key}"
    for key, value in args.items():
        if key not in props:
            return f"unknown argument {key}"
        spec = props[key]
        expected = spec.get("type")
        if expected:
            py = _JSON_TYPES[expected]
            if isinstance(value, bool) and expected in ("integer", "number"):
                return f"argument {key} must be {expected}"
            if not isinstance(value, py):
                return f"argument {key} must be {expected}"
        if "enum" in spec and value not in spec["enum"]:
            return f"argument {key} must be one of {spec['enum']}"
        if "maximum" in spec and isinstance(value, int | float) and value > spec["maximum"]:
            return f"argument {key} must be at most {spec['maximum']}"
        if "minimum" in spec and isinstance(value, int | float) and value < spec["minimum"]:
            return f"argument {key} must be at least {spec['minimum']}"
        if "maxLength" in spec and isinstance(value, str) and len(value) > spec["maxLength"]:
            return f"argument {key} is longer than {spec['maxLength']} characters"
    return None
