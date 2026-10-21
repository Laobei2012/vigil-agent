"""Entry points: vigil-agent orchestrator | receiver | gateway | migrate."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys

import uvicorn
from fastapi import FastAPI

from vigil_agent.catalog import Catalog, Flags, ReloadingFile, load_flags
from vigil_agent.config import Settings

log = logging.getLogger("vigil")


def _logging() -> None:
    logging.basicConfig(level=os.environ.get("VIGIL_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_orchestrator(s: Settings):
    from vigil_agent.agent.model import BedrockModel
    from vigil_agent.gateway.app import GatewayClient
    from vigil_agent.intake.receiver import InboxQueue, SqsQueue, create_receiver
    from vigil_agent.orchestrator.intake import Intake
    from vigil_agent.orchestrator.investigation import Investigator
    from vigil_agent.orchestrator.publisher import Publisher
    from vigil_agent.orchestrator.runtime import Runtime
    from vigil_agent.pagerduty import PagerDutyClient
    from vigil_agent.slack.client import DryRunSlack, SlackClient
    from vigil_agent.state.store import Store

    store = Store(s.database_url)
    catalog = ReloadingFile(s.catalog_path, Catalog.load, Catalog([]))
    flags = ReloadingFile(s.flags_path, load_flags, Flags())
    pd = PagerDutyClient(s.pd_api_key, s.pd_api_url)
    slack = SlackClient(s.slack_bot_token) if (s.slack_bot_token and not s.slack_dry_run) else DryRunSlack(store)
    publisher = Publisher(s, store, slack)
    gateway = GatewayClient(s.gateway_url, s.gateway_token)
    model = BedrockModel(s.model_id, s.bedrock_region, s.model_effort) if s.model_enabled else None
    intake = Intake(s, store, catalog, flags, pd, publisher)
    investigator = Investigator(s, store, catalog, gateway, model, publisher, pd, s.owner)
    runtime = Runtime(s, store, intake, investigator, publisher)
    queue = InboxQueue(store) if s.queue == "direct" else SqsQueue(s.sqs_queue_url, s.aws_region)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.open()
        await store.migrate()
        catalog.get()  # fail fast on an invalid catalog
        runtime.start(intake=s.queue == "direct")
        sqs_task = None
        if s.queue == "sqs":
            sqs_task = asyncio.create_task(runtime.sqs_loop(queue.client, s.sqs_queue_url))
        socket = None
        if s.slack_app_token and s.slack_bot_token and not s.slack_dry_run:
            from vigil_agent.slack.interactivity import start_socket_mode

            socket = await start_socket_mode(s.slack_app_token, s.slack_bot_token, store)
        log.info("orchestrator %s started: cluster=%s model=%s slack=%s queue=%s", s.owner, s.cluster,
                 s.model_id if model else "off", type(slack).__name__, s.queue)
        yield
        await runtime.stop()
        if sqs_task:
            sqs_task.cancel()
        if socket:
            await socket.close()
        await store.close()

    app = FastAPI(title="vigil-agent orchestrator", lifespan=lifespan)
    # In the test cluster the receiver runs in the orchestrator pod and writes to the inbox.
    create_receiver(s, queue, app)
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="vigil-agent")
    parser.add_argument("command", choices=["orchestrator", "receiver", "gateway", "migrate"])
    args = parser.parse_args(argv)
    _logging()
    if args.command == "gateway":
        from vigil_agent.gateway.app import create_app
        from vigil_agent.gateway.settings import GatewaySettings

        gs = GatewaySettings()
        uvicorn.run(create_app(gs), host="0.0.0.0", port=gs.port, log_level="warning")  # noqa: S104
        return
    s = Settings()
    if args.command == "migrate":
        from vigil_agent.state.store import Store

        async def migrate():
            store = Store(s.database_url)
            await store.open()
            await store.migrate()
            await store.close()

        asyncio.run(migrate())
        return
    if args.command == "receiver":
        from vigil_agent.intake.receiver import SqsQueue, create_receiver

        app = create_receiver(s, SqsQueue(s.sqs_queue_url, s.aws_region))
    else:
        app = build_orchestrator(s)
    uvicorn.run(app, host="0.0.0.0", port=s.http_port, log_level="warning")  # noqa: S104


if __name__ == "__main__":
    main()
