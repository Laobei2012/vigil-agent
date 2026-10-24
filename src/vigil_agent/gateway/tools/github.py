"""GitHub history tools (REST API, read-only token) and deployed-revision lookup."""

from __future__ import annotations

import fnmatch
import time
from datetime import datetime

import httpx

from vigil_agent.catalog import Catalog
from vigil_agent.gateway.core import Tool, ToolError, ToolOutput, check_segment
from vigil_agent.gateway.settings import GatewaySettings
from vigil_agent.gateway.tools.kubernetes import KubernetesTools
from vigil_agent.timeutil import iso, parse_time

MAX_CHANGES = 50
MAX_DIFF_LINES = 2000
REVISION_LABEL = "org.opencontainers.image.revision"


class GitHubTools:
    def __init__(
        self, settings: GatewaySettings, client: httpx.AsyncClient, k8s: KubernetesTools, catalog: Catalog | None
    ):
        self.s = settings
        self.http = client
        self.k8s = k8s
        self.catalog = catalog
        self._sha_cache: dict[str, tuple[float, dict]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.s.github_token)

    def _headers(self, accept: str = "application/vnd.github+json") -> dict:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.s.github_token:
            h["Authorization"] = f"Bearer {self.s.github_token}"
        return h

    def repo(self, repo: str | None) -> str:
        if not repo:
            if len(self.s.repos) == 1:
                return self.s.repos[0]
            raise ToolError("invalid_arguments", f"repo is required, one of {self.s.repos}")
        if repo not in self.s.repos:
            raise ToolError("forbidden", f"repository {repo} is not in the allowlist")
        return repo

    async def _get(self, path: str, params: dict | None = None, accept: str | None = None):
        if not self.enabled:
            raise ToolError("not_configured", "GitHub access is not configured in this environment")
        resp = await self.http.get(
            f"{self.s.github_api_url}{path}",
            params=params,
            headers=self._headers(accept or "application/vnd.github+json"),
            timeout=20,
        )
        if resp.status_code == 404:
            raise ToolError("not_found", f"not found: {path}")
        if resp.status_code >= 400:
            raise ToolError("backend_error", f"github {resp.status_code}: {resp.text[:300]}")
        return resp.text if accept and "diff" in accept else resp.json()

    async def changes(self, repo: str, since: datetime, until: datetime) -> list[dict]:
        """Commits, merged PRs, deployments, and Terraform workflow runs in a window."""
        out: list[dict] = []
        commits = await self._get(
            f"/repos/{repo}/commits", {"since": iso(since), "until": iso(until), "per_page": MAX_CHANGES}
        )
        for c in commits:
            out.append(
                {
                    "time": c["commit"]["committer"]["date"],
                    "source": "github_commit",
                    "repo": repo,
                    "sha": c["sha"][:12],
                    "author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
                    "summary": c["commit"]["message"].splitlines()[0][:200],
                    "url": c.get("html_url"),
                }
            )
        pulls = await self._get(
            f"/repos/{repo}/pulls", {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 50}
        )
        for p in pulls:
            merged = p.get("merged_at")
            if merged and since <= parse_time(merged) <= until:
                out.append(
                    {
                        "time": merged,
                        "source": "github_pr",
                        "repo": repo,
                        "number": p["number"],
                        "author": (p.get("user") or {}).get("login"),
                        "summary": p["title"][:200],
                        "sha": (p.get("merge_commit_sha") or "")[:12],
                        "url": p.get("html_url"),
                    }
                )
        deployments = await self._get(f"/repos/{repo}/deployments", {"per_page": 30})
        for d in deployments:
            if since <= parse_time(d["created_at"]) <= until:
                out.append(
                    {
                        "time": d["created_at"],
                        "source": "github_deployment",
                        "repo": repo,
                        "sha": d["sha"][:12],
                        "summary": f"deploy to {d.get('environment')}",
                        "url": d.get("url"),
                    }
                )
        if self.s.github_terraform_workflow:
            runs = await self._get(
                f"/repos/{repo}/actions/workflows/{self.s.github_terraform_workflow}/runs",
                {"created": f">={iso(since)}", "per_page": 30},
            )
            for r in runs.get("workflow_runs", []):
                out.append(
                    {
                        "time": r["created_at"],
                        "source": "terraform_run",
                        "repo": repo,
                        "sha": r["head_sha"][:12],
                        "summary": f"{r['name']} {r['event']} on "
                        f"{r['head_branch']}: {r.get('conclusion') or r.get('status')}",
                        "url": r.get("html_url"),
                    }
                )
        out.sort(key=lambda e: e["time"], reverse=True)
        return out

    async def list_changes(self, since: str = "now-24h", until: str = "now", repo: str | None = None) -> ToolOutput:
        repo = self.repo(repo)
        t0, t1 = parse_time(since), parse_time(until)
        if (t1 - t0).total_seconds() > 7 * 86400:
            raise ToolError("invalid_arguments", "window is longer than 7 days")
        out = await self.changes(repo, t0, t1)
        return ToolOutput(
            {"repo": repo, "changes": out[:MAX_CHANGES], "count": len(out)},
            f"https://github.com/{repo}/commits",
            len(out) > MAX_CHANGES,
        )

    async def get_diff(
        self, base: str, head: str, repo: str | None = None, path_filter: str | None = None
    ) -> ToolOutput:
        repo = self.repo(repo)
        check_segment("git_ref", base, "base")
        check_segment("git_ref", head, "head")
        text = await self._get(f"/repos/{repo}/compare/{base}...{head}", accept="application/vnd.github.diff")
        if path_filter:
            parts = text.split("diff --git ")
            keep = [p for p in parts[1:] if fnmatch.fnmatch(p.split(" ", 1)[0].removeprefix("a/"), path_filter)]
            text = "".join("diff --git " + p for p in keep)
        lines = text.splitlines()
        return ToolOutput(
            {"repo": repo, "base": base, "head": head, "lines": len(lines), "diff": "\n".join(lines[:MAX_DIFF_LINES])},
            f"https://github.com/{repo}/compare/{base}...{head}",
            len(lines) > MAX_DIFF_LINES,
        )

    async def resolve_deployed_sha(self, service: str | None = None, namespace: str | None = None) -> ToolOutput:
        key = f"{service}/{namespace}"
        cached = self._sha_cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            return ToolOutput(cached[1])
        if self.s.codebase_backend == "flux":
            ks = await self.k8s._get(
                f"/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/{self.s.flux_source_namespace}"
                f"/kustomizations/{self.s.flux_kustomization}"
            )
            rev = ks.get("status", {}).get("lastAppliedRevision", "")
            sha = rev.rsplit(":", 1)[-1] if rev else ""
            if not sha:
                raise ToolError("not_found", "the Flux Kustomization has no applied revision")
            result = {"repo": self.s.flux_repo, "sha": sha, "source": "flux lastAppliedRevision", "revision": rev}
        else:
            result = await self._sha_from_images(service, namespace)
        self._sha_cache[key] = (time.monotonic(), result)
        return ToolOutput(result)

    async def _sha_from_images(self, service: str | None, namespace: str | None) -> dict:
        namespaces = [namespace] if namespace else []
        repos: list[str] = []
        if self.catalog and service and (entry := self.catalog.by_name(service)):
            namespaces = namespaces or entry.namespaces
            repos = entry.repos
        if not namespaces:
            raise ToolError("invalid_arguments", "unknown service; pass a namespace")
        images: set[str] = set()
        for ns in namespaces:
            self.k8s._check_ns(ns, True)
            pods = await self.k8s._get(f"/api/v1/namespaces/{ns}/pods", {"limit": 50})
            for p in pods.get("items", []):
                for c in p.get("status", {}).get("containerStatuses", []) or []:
                    images.add(c.get("imageID") or c.get("image"))
        for image in sorted(i for i in images if i):
            labels = await image_labels(self.http, image)
            if labels.get(REVISION_LABEL):
                return {
                    "repo": repos[0] if repos else None,
                    "sha": labels[REVISION_LABEL],
                    "source": f"image label of {image}",
                    "image": image,
                }
        raise ToolError(
            "not_found",
            f"no running image in {namespaces} carries the label {REVISION_LABEL}; "
            "add it in CI to onboard this service",
        )

    def tools(self) -> list[Tool]:
        repo = {"type": "string", "description": f"one of {self.s.repos}"}
        when = {"type": "string", "description": "RFC 3339 or now-<n><m|h|d>"}
        tools = [
            Tool(
                "resolve_deployed_sha",
                "Find the commit that is running now for a service. All code reads must use this SHA.",
                {"type": "object", "properties": {"service": {"type": "string"}, "namespace": {"type": "string"}}},
                "github",
                self.resolve_deployed_sha,
            ),
        ]
        if self.enabled:
            tools += [
                Tool(
                    "list_changes",
                    "List commits, merged PRs, deployment records, and Terraform runs in a "
                    "window (max 7 days, 50 results).",
                    {"type": "object", "properties": {"since": when, "until": when, "repo": repo}},
                    "github",
                    self.list_changes,
                ),
                Tool(
                    "get_diff",
                    "Get the diff between two commits, truncated to 2000 lines. path_filter is a "
                    "glob such as gitops/tools/*.",
                    {
                        "type": "object",
                        "properties": {
                            "base": {"type": "string"},
                            "head": {"type": "string"},
                            "repo": repo,
                            "path_filter": {"type": "string"},
                        },
                        "required": ["base", "head"],
                    },
                    "github",
                    self.get_diff,
                ),
            ]
        return tools


async def image_labels(http: httpx.AsyncClient, image: str) -> dict:
    """Read the config labels of an image from its registry, with an anonymous pull token."""
    ref = image.removeprefix("docker-pullable://")
    if "@" in ref:
        name, digest = ref.split("@", 1)
        reference = digest
    else:
        name, _, reference = ref.rpartition(":")
        if not name or "/" in reference:
            name, reference = ref, "latest"
    registry, _, repo = name.partition("/")
    if "." not in registry and ":" not in registry:
        registry, repo = "registry-1.docker.io", name if "/" in name else f"library/{name}"
    if registry == "docker.io":
        registry = "registry-1.docker.io"
    base = f"https://{registry}/v2/{repo}"
    accept = (
        "application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,"
        "application/vnd.docker.distribution.manifest.v2+json,"
        "application/vnd.docker.distribution.manifest.list.v2+json"
    )
    headers = {"Accept": accept}
    resp = await http.get(f"{base}/manifests/{reference}", headers=headers, timeout=15)
    if resp.status_code == 401 and "Bearer" in resp.headers.get("www-authenticate", ""):
        params = dict(p.split("=", 1) for p in resp.headers["www-authenticate"][7:].replace('"', "").split(","))
        tok = await http.get(
            params["realm"], params={"service": params.get("service"), "scope": params.get("scope")}, timeout=15
        )
        headers["Authorization"] = f"Bearer {tok.json().get('token') or tok.json().get('access_token')}"
        resp = await http.get(f"{base}/manifests/{reference}", headers=headers, timeout=15)
    if resp.status_code >= 400:
        return {}
    manifest = resp.json()
    if "manifests" in manifest:
        arm = next(
            (m for m in manifest["manifests"] if m.get("platform", {}).get("architecture") == "arm64"),
            manifest["manifests"][0],
        )
        resp = await http.get(f"{base}/manifests/{arm['digest']}", headers=headers, timeout=15)
        manifest = resp.json()
    cfg = await http.get(
        f"{base}/blobs/{manifest['config']['digest']}", headers=headers, timeout=15, follow_redirects=True
    )
    if cfg.status_code >= 400:
        return {}
    return (cfg.json().get("config") or {}).get("Labels") or {}
