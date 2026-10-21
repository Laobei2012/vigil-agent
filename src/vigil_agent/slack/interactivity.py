"""Socket Mode handler for the feedback buttons. Outbound only, so no second public ingress."""

from __future__ import annotations

import logging

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from vigil_agent import telemetry

log = logging.getLogger("vigil.interactivity")

RATINGS = {"vigil_helpful": "helpful", "vigil_not_helpful": "not_helpful"}


async def handle_action(store, action: dict, user: str | None) -> str | None:
    action_id = action.get("action_id")
    inv_id = int(action.get("value", "0") or 0)
    if action_id in RATINGS:
        await store.add_feedback(inv_id, RATINGS[action_id], user)
        telemetry.FEEDBACK.labels(rating=RATINGS[action_id]).inc()
        return RATINGS[action_id]
    if action_id == "vigil_reinvestigate":
        # Run the deep pass again; the first reply stays.
        n = await store.execute(
            "UPDATE investigations SET status = 'queued', step = 'fast_done', deep_report = NULL, resolved = false "
            "WHERE id = %s AND status IN ('done', 'failed')",
            (inv_id,),
        )
        await store.add_feedback(inv_id, "reinvestigate", user)
        return "reinvestigate" if n else "busy"
    return None


async def start_socket_mode(app_token: str, bot_token: str, store) -> SocketModeClient:
    client = SocketModeClient(app_token=app_token, web_client=AsyncWebClient(token=bot_token))

    async def listener(c: SocketModeClient, req: SocketModeRequest) -> None:
        await c.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "interactive" or req.payload.get("type") != "block_actions":
            return
        user = (req.payload.get("user") or {}).get("id")
        for action in req.payload.get("actions", []):
            try:
                await handle_action(store, action, user)
            except Exception:
                log.exception("action %s failed", action.get("action_id"))

    client.socket_mode_request_listeners.append(listener)
    await client.connect()
    return client
