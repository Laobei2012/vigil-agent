"""Claude through the Anthropic SDK: on Amazon Bedrock (InvokeModel) or through the Anthropic API.

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

    async def create(
        self, *, system: list[dict], tools: list[dict], messages: list[dict], max_tokens: int
    ) -> Message: ...


class ClaudeModel:
    """One Messages API call with retries. The client decides where the call goes."""

    def __init__(self, model_id: str, client, effort: str = "high", retries: int = 4, sleep=asyncio.sleep):
        self.model_id = model_id
        self.client = client
        self.effort = effort
        self.retries = retries
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
            except anthropic.APIStatusError as exc:
                # A bad key, model ID, or request does not get better with a retry.
                raise ModelUnavailable(f"{type(exc).__name__} {exc.status_code}: {exc.message}"[:500]) from exc
        raise ModelUnavailable("unreachable")


class BedrockModel(ClaudeModel):
    """Claude on Amazon Bedrock (InvokeModel), the production path."""

    def __init__(
        self, model_id: str, region: str, effort: str = "high", retries: int = 4, client=None, sleep=asyncio.sleep
    ):
        client = client or anthropic.AsyncAnthropicBedrock(aws_region=region, max_retries=0, timeout=300)
        super().__init__(model_id, client, effort, retries, sleep)


class AnthropicModel(ClaudeModel):
    """Claude through the Anthropic API. The client reads ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL."""

    def __init__(self, model_id: str, effort: str = "high", retries: int = 4, client=None, sleep=asyncio.sleep):
        client = client or anthropic.AsyncAnthropic(max_retries=0, timeout=300)
        super().__init__(model_id, client, effort, retries, sleep)


def build_model(settings) -> ClaudeModel:
    if settings.model_provider == "anthropic":
        return AnthropicModel(settings.model_id, settings.model_effort)
    if settings.model_provider == "bedrock":
        return BedrockModel(settings.model_id, settings.bedrock_region, settings.model_effort)
    raise ValueError(f"unknown model provider {settings.model_provider!r}, use bedrock or anthropic")


def with_cache_points(system_text: str, tools: list[dict]) -> tuple[list[dict], list[dict]]:
    """Mark the end of the stable prefix (tools, then system) for prompt caching."""
    system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    return system, [dict(t) for t in tools]
