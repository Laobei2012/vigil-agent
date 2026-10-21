"""The tool gateway HTTP service.

GET /v1/tools returns the schemas of the enabled tools. POST /v1/tools/{name}
runs one tool. Every call needs the shared bearer token, because the test
cluster cannot enforce a NetworkPolicy.
"""

from __future__ import annotations

import hmac
import logging

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from vigil_agent.catalog import Catalog
from vigil_agent.gateway.core import Registry, TokenBucket, load_allowlist
from vigil_agent.gateway.redaction import Redactor
from vigil_agent.gateway.settings import GatewaySettings
from vigil_agent.gateway.tools.atlassian import ConfluenceTools, JiraTools
from vigil_agent.gateway.tools.codebase import CodebaseTools
from vigil_agent.gateway.tools.github import GitHubTools
from vigil_agent.gateway.tools.kubernetes import KubernetesTools
from vigil_agent.gateway.tools.logs import ElasticsearchLogs, LokiLogs
from vigil_agent.gateway.tools.metrics import MetricsTools
from vigil_agent.gateway.tools.timeline import TimelineTools

log = logging.getLogger("vigil.gateway")


def build_registry(
    s: GatewaySettings, http: httpx.AsyncClient | None = None, k8s: KubernetesTools | None = None
) -> Registry:
    http = http or httpx.AsyncClient(timeout=30, headers={"User-Agent": "vigil-agent-gateway"})
    catalog = Catalog.load(s.catalog_path) if s.catalog_path else None
    backends = ("metrics", "logs", "kubernetes", "github", "codebase", "jira", "confluence", "timeline")
    reg = Registry(
        allowlist=load_allowlist(),
        redactor=Redactor(pii=s.pii_redaction),
        rate_limits={b: TokenBucket(s.rate_limit_per_minute, s.rate_limit_burst) for b in backends},
    )
    k8s = k8s or KubernetesTools(s)
    github = GitHubTools(s, http, k8s, catalog)
    groups = [k8s.tools(), github.tools(), TimelineTools(s, k8s, github, catalog).tools()]
    if s.metrics_url:
        groups.append(MetricsTools(s, http).tools())
    if s.logs_backend == "loki" and s.loki_url:
        groups.append(LokiLogs(s, http).tools())
    elif s.logs_backend == "elasticsearch" and s.es_url:
        groups.append(ElasticsearchLogs(s, http).tools())
    if s.codebase_backend == "flux" or s.github_token:
        groups.append(CodebaseTools(s, http, k8s).tools())
    if s.jira_url:
        groups.append(JiraTools(s, http).tools())
    if s.confluence_url:
        groups.append(ConfluenceTools(s, http).tools())
    for group in groups:
        for tool in group:
            reg.register(tool)
    log.info("gateway tools: %s", ", ".join(sorted(reg.tools)))
    return reg


class CallRequest(BaseModel):
    args: dict = {}
    incident_id: str | None = None


def create_app(settings: GatewaySettings | None = None, registry: Registry | None = None) -> FastAPI:
    s = settings or GatewaySettings()
    reg = registry or build_registry(s)
    app = FastAPI(title="vigil-agent tool gateway")

    def auth(authorization: str | None = Header(default=None)) -> None:
        if not s.token:
            return
        expected = f"Bearer {s.token}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "tools": len(reg.tools)}

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/tools", dependencies=[Depends(auth)])
    async def tools() -> list[dict]:
        return reg.schemas()

    @app.post("/v1/tools/{name}", dependencies=[Depends(auth)])
    async def call(name: str, req: CallRequest) -> dict:
        result = await reg.call(name, req.args, req.incident_id)
        return result.to_dict()

    return app


class GatewayClient:
    """Used by the orchestrator. The model never talks to the gateway directly."""

    def __init__(self, base_url: str, token: str | None = None, http: httpx.AsyncClient | None = None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = http or httpx.AsyncClient(base_url=base_url, headers=headers, timeout=70)
        self._schemas: list[dict] | None = None

    async def schemas(self) -> list[dict]:
        if self._schemas is None:
            resp = await self.http.get("/v1/tools")
            resp.raise_for_status()
            self._schemas = resp.json()
        return self._schemas

    async def call(self, name: str, args: dict, incident_id: str | None) -> dict:
        try:
            resp = await self.http.post(f"/v1/tools/{name}", json={"args": args, "incident_id": incident_id})
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"gateway unreachable: {exc}", "error_code": "gateway_error"}
        if resp.status_code >= 400:
            return {"ok": False, "error": f"gateway {resp.status_code}", "error_code": "gateway_error"}
        return resp.json()
