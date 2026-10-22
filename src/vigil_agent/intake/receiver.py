"""The webhook receiver. It verifies, enqueues, and returns 2xx in well under a second.

POST /webhooks/pagerduty    PagerDuty V3 webhook, X-PagerDuty-Signature HMAC verified.
POST /webhooks/alertmanager Alertmanager webhook_configs (in-cluster), bearer token. 503 without a token.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from vigil_agent import telemetry
from vigil_agent.config import Settings
from vigil_agent.intake.events import IntakeEvent, parse_alertmanager, parse_pd_webhook, verify_pd_signature

log = logging.getLogger("vigil.receiver")

# Webhook bodies are a few KB. The cap applies before the signature test.
MAX_BODY_BYTES = 1_000_000


class Queue(Protocol):
    async def put(self, event: IntakeEvent) -> None: ...


class InboxQueue:
    """Test cluster: the receiver writes the event to the PostgreSQL inbox."""

    def __init__(self, store):
        self.store = store

    async def put(self, event: IntakeEvent) -> None:
        await self.store.inbox_put(event.event_id, event.incident_id, event.source, event.model_dump(mode="json"))


class SqsQueue:
    """Production: SQS FIFO, one message group per incident, deduplicated on the event ID."""

    def __init__(self, queue_url: str, region: str, client=None):
        import boto3

        self.url = queue_url
        self.client = client or boto3.client("sqs", region_name=region)

    async def put(self, event: IntakeEvent) -> None:
        await asyncio.to_thread(
            self.client.send_message,
            QueueUrl=self.url,
            MessageBody=event.model_dump_json(),
            MessageGroupId=event.incident_id,
            MessageDeduplicationId=event.event_id[:128],
        )


def create_receiver(settings: Settings, queue: Queue, app: FastAPI | None = None) -> FastAPI:
    app = app or FastAPI(title="vigil-agent receiver")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/webhooks/pagerduty")
    async def pagerduty(request: Request):
        if not settings.pd_secrets:
            return JSONResponse({"error": "PagerDuty intake is not configured"}, status_code=503)
        body = await _read_body(request)
        if body is None:
            return JSONResponse({"error": "body too large"}, status_code=413)
        if not verify_pd_signature(body, request.headers.get("x-pagerduty-signature"), settings.pd_secrets):
            telemetry.INTAKE_EVENTS.labels(source="pagerduty", result="bad_signature").inc()
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            event = parse_pd_webhook(json.loads(body))
        except (ValueError, KeyError) as exc:
            log.warning("bad PagerDuty body: %s", exc)
            return JSONResponse({"error": "bad body"}, status_code=400)
        if event is None:
            return JSONResponse({"status": "ignored"}, status_code=202)
        return await _enqueue(event)

    @app.post("/webhooks/alertmanager")
    async def alertmanager(request: Request):
        if not settings.am_webhook_token:
            return JSONResponse({"error": "Alertmanager intake is not configured"}, status_code=503)
        got = request.headers.get("authorization", "")
        # Compare bytes, because compare_digest raises on a str with non-ASCII characters.
        if not hmac.compare_digest(got.encode(), f"Bearer {settings.am_webhook_token}".encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await _read_body(request)
        if body is None:
            return JSONResponse({"error": "body too large"}, status_code=413)
        try:
            event = parse_alertmanager(json.loads(body))
        except (ValueError, KeyError) as exc:
            return JSONResponse({"error": f"bad body: {exc}"}, status_code=400)
        return await _enqueue(event)

    async def _read_body(request: Request) -> bytes | None:
        """The request body, or None when it is larger than MAX_BODY_BYTES."""
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                return None
        return bytes(body)

    async def _enqueue(event: IntakeEvent):
        try:
            await queue.put(event)
        except Exception:
            log.exception("enqueue failed for %s", event.event_id)
            # A 5xx makes PagerDuty or Alertmanager send the event again.
            return JSONResponse({"error": "enqueue failed"}, status_code=500)
        telemetry.INTAKE_EVENTS.labels(source=event.source, result="enqueued").inc()
        return JSONResponse({"status": "accepted", "event_id": event.event_id}, status_code=202)

    return app
