"""Slack posting. The real client retries 429 with Retry-After and 5xx with backoff.

The dry-run client writes each message to the slack_outbox table instead,
which is rollout stage 1 (no posting) and the mode used until the Slack app
exists.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from vigil_agent.gateway.redaction import Redactor
from vigil_agent.slack.render import split_messages

log = logging.getLogger("vigil.slack")


class SlackPoster(Protocol):
    async def post(
        self, channel: str, text: str, blocks: list[dict] | None = None, thread_ts: str | None = None
    ) -> str: ...

    async def update(self, channel: str, ts: str, text: str, blocks: list[dict] | None = None) -> None: ...


async def post_blocks(
    slack: SlackPoster, channel: str, text: str, blocks: list[dict], thread_ts: str | None
) -> list[str]:
    """Post blocks as one or more messages, 50 blocks each. Returns the message timestamps."""
    out = []
    for i, part in enumerate(split_messages(blocks)):
        out.append(await slack.post(channel, text if i == 0 else f"{text} (part {i + 1})", part, thread_ts))
    return out


class RedactingSlack:
    """Redacts the text and blocks of each message, as a last guard before Slack."""

    def __init__(self, inner: SlackPoster):
        self.inner = inner
        self.redactor = Redactor()

    def __getattr__(self, name: str):
        # channel_ids and other attributes of the wrapped client.
        return getattr(self.inner, name)

    async def post(self, channel, text, blocks=None, thread_ts=None) -> str:
        return await self.inner.post(channel, self.redactor.text(text), self.redactor.value(blocks), thread_ts)

    async def update(self, channel, ts, text, blocks=None) -> None:
        await self.inner.update(channel, ts, self.redactor.text(text), self.redactor.value(blocks))


class SlackClient:
    def __init__(self, token: str, client: AsyncWebClient | None = None, max_retries: int = 4, sleep=asyncio.sleep):
        self.client = client or AsyncWebClient(token=token)
        # chat.update needs a channel ID. chat.postMessage returns it for a channel name.
        self.channel_ids: dict[str, str] = {}
        self.max_retries = max_retries
        self.sleep = sleep

    async def _call(self, method: str, **kwargs):
        for attempt in range(self.max_retries + 1):
            try:
                return await getattr(self.client, method)(**kwargs)
            except SlackApiError as exc:
                status = exc.response.status_code
                if attempt == self.max_retries or not (status == 429 or status >= 500):
                    raise
                delay = float(exc.response.headers.get("Retry-After", 2**attempt)) if status == 429 else 2**attempt
                log.warning("slack %s returned %s, retry in %.0fs", method, status, delay)
                await self.sleep(delay)
        raise RuntimeError("unreachable")

    async def post(self, channel, text, blocks=None, thread_ts=None) -> str:
        resp = await self._call(
            "chat_postMessage",
            channel=channel,
            text=text,
            blocks=blocks,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
        if resp.get("channel"):
            self.channel_ids[channel] = resp["channel"]
        return resp["ts"]

    async def update(self, channel, ts, text, blocks=None) -> None:
        await self._call("chat_update", channel=channel, ts=ts, text=text, blocks=blocks)


class DryRunSlack:
    def __init__(self, store):
        self.store = store
        self._seq = 0

    def _ts(self) -> str:
        self._seq += 1
        return f"{time.time():.0f}.{self._seq:06d}"

    async def post(self, channel, text, blocks=None, thread_ts=None) -> str:
        ts = self._ts()
        await self.store.outbox_add("post", channel, ts, thread_ts, text, blocks)
        log.info("dry-run slack post channel=%s thread=%s ts=%s text=%s", channel, thread_ts, ts, text[:200])
        return ts

    async def update(self, channel, ts, text, blocks=None) -> None:
        await self.store.outbox_add("update", channel, ts, None, text, blocks)
        log.info("dry-run slack update channel=%s ts=%s text=%s", channel, ts, text[:200])
