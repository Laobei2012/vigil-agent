"""Claude on Amazon Bedrock through the Anthropic SDK's Bedrock client (InvokeModel).

The request body is the Anthropic Messages format, so tool use and prompt
caching need no translation layer and recorded fixtures share one format.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import anthropic
from anthropic.types import Message

from vigil_agent import telemetry

log = logging.getLogger("vigil.model")


class ModelUnavailable(Exception):
    """Throttled or down after retries. The orchestrator switches to degraded mode."""


class ModelClient(Protocol):
    model_id: str

    async def create(self, *, system: list[dict], tools: list[dict], messages: list[dict],
                     max_tokens: int) -> Message: ...


class BedrockModel:
    def __init__(self, model_id: str, region: str, effort: str = "high", retries: int = 4,
                 client: anthropic.AsyncAnthropicBedrock | None = None, sleep=asyncio.sleep):
        self.model_id = model_id
        self.effort = effort
        self.retries = retries
        self.client = client or anthropic.AsyncAnthropicBedrock(aws_region=region, max_retries=0, timeout=300)
        self.sleep = sleep

    async def create(self, *, system, tools, messages, max_tokens) -> Message:
        for attempt in range(self.retries + 1):
            try:
                return await self.client.messages.create(
                    model=self.model_id,
                    max_tokens=max_tokens,
                    system=system,
                    tools=tools,
                    messages=messages,
                    # Opus 5.5 always thinks; effort is the depth control.
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.effort},
                )
            except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as exc:
                if isinstance(exc, anthropic.RateLimitError):
                    telemetry.BEDROCK_THROTTLES.inc()
                if attempt == self.retries:
                    raise ModelUnavailable(f"{type(exc).__name__}: {exc}") from exc
                delay = 2 ** (attempt + 1)
                log.warning("model call failed (%s), retry in %ss", type(exc).__name__, delay)
                await self.sleep(delay)
        raise ModelUnavailable("unreachable")


def with_cache_points(system_text: str, tools: list[dict]) -> tuple[list[dict], list[dict]]:
    """Mark the end of the stable prefix (tools, then system) for prompt caching."""
    system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    return system, [dict(t) for t in tools]
