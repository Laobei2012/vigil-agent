"""get_change_timeline: merge read-only change sources into one ordered list.

Sources: GitHub commits, PRs, deployments, and Terraform runs; ReplicaSet
rollout history (kept beyond the 1-hour event retention); Kubernetes events;
and Flux HelmRelease history and Kustomization revisions. A failing source is
reported as a gap, not a failure.
"""

from __future__ import annotations

import asyncio

from vigil_agent.catalog import Catalog
from vigil_agent.gateway.core import Tool, ToolError, ToolOutput
from vigil_agent.gateway.settings import GatewaySettings
from vigil_agent.gateway.tools.github import GitHubTools
from vigil_agent.gateway.tools.kubernetes import KubernetesTools
from vigil_agent.timeutil import parse_time, utcnow

MAX_ENTRIES = 100
MAX_WINDOW_DAYS = 7


class TimelineTools:
    def __init__(self, settings: GatewaySettings, k8s: KubernetesTools, github: GitHubTools,
                 catalog: Catalog | None):
        self.s = settings
        self.k8s = k8s
        self.github = github
        self.catalog = catalog

    def _scope(self, service: str | None, namespace: str | None) -> tuple[list[str], list[str]]:
        entry = self.catalog.by_name(service) if (self.catalog and service) else None
        namespaces = [namespace] if namespace else (entry.namespaces if entry else [])
        repos = entry.repos if entry else self.s.repos
        if not namespaces and not repos:
            raise ToolError("invalid_arguments", "unknown service; pass a namespace")
        return namespaces, [r for r in repos if r in self.s.repos or not self.s.repos]

    async def get_change_timeline(self, service: str | None = None, since: str = "now-2h",
                                  namespace: str | None = None) -> ToolOutput:
        t0 = parse_time(since)
        now = utcnow()
        if (now - t0).total_seconds() > MAX_WINDOW_DAYS * 86400:
            raise ToolError("invalid_arguments", f"window is longer than {MAX_WINDOW_DAYS} days")
        namespaces, repos = self._scope(service, namespace)
        jobs: dict[str, asyncio.Future] = {}
        for repo in repos if self.github.enabled else []:
            jobs[f"github:{repo}"] = self.github.changes(repo, t0, now)
        for ns in namespaces:
            jobs[f"rollouts:{ns}"] = self._rollouts(ns, t0)
            jobs[f"events:{ns}"] = self._events(ns, t0)
            jobs[f"helmreleases:{ns}"] = self._helmreleases(ns, t0)
        jobs["flux"] = self._flux(t0, namespaces)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        entries, gaps = [], []
        if repos and not self.github.enabled:
            gaps.append("github: not configured, commits and PRs are missing")
        for name, res in zip(jobs, results, strict=True):
            if isinstance(res, BaseException):
                gaps.append(f"{name}: {res}"[:300])
            else:
                entries.extend(res)
        entries = [e for e in entries if e.get("time") and parse_time(e["time"]) >= t0]
        entries.sort(key=lambda e: e["time"], reverse=True)
        return ToolOutput({"since": since, "namespaces": namespaces, "repos": repos,
                           "entries": entries[:MAX_ENTRIES], "count": len(entries), "gaps": gaps},
                          None, len(entries) > MAX_ENTRIES)

    async def _rollouts(self, ns: str, t0) -> list[dict]:
        data = await self.k8s._get(f"/apis/apps/v1/namespaces/{ns}/replicasets", {"limit": 200})
        out = []
        for rs in data.get("items", []):
            meta = rs["metadata"]
            ts = meta.get("creationTimestamp")
            if not ts or parse_time(ts) < t0:
                continue
            owner = next((o["name"] for o in meta.get("ownerReferences", []) if o.get("kind") == "Deployment"),
                         meta["name"])
            images = [c.get("image") for c in rs["spec"]["template"]["spec"].get("containers", [])]
            rev = (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision")
            out.append({"time": ts, "source": "k8s_rollout", "namespace": ns,
                        "summary": f"Deployment {owner} revision {rev}: {', '.join(images)}"})
        return out

    async def _events(self, ns: str, t0) -> list[dict]:
        events = await self.k8s._events(ns, None, None, None)
        keep = {"Killing", "BackOff", "Failed", "FailedScheduling", "Evicted", "OOMKilling", "Unhealthy",
                "ScalingReplicaSet", "SuccessfulRescale", "FailedMount", "NodeNotReady", "Preempting"}
        return [{"time": e["time"], "source": "k8s_event", "namespace": ns,
                 "summary": f"{e['type']} {e['reason']} {e['object']}: {e['message'][:200]}"}
                for e in events if e["reason"] in keep or e["type"] == "Warning"]

    async def _helmreleases(self, ns: str, t0) -> list[dict]:
        try:
            data = await self.k8s._get(f"/apis/helm.toolkit.fluxcd.io/v2/namespaces/{ns}/helmreleases")
        except ToolError as exc:
            if exc.code == "not_found":
                return []
            raise
        out = []
        for hr in data.get("items", []):
            for h in hr.get("status", {}).get("history", []) or []:
                ts = h.get("lastDeployed") or h.get("firstDeployed")
                if ts:
                    out.append({"time": ts, "source": "helm_release", "namespace": ns,
                                "summary": f"HelmRelease {hr['metadata']['name']} chart {h.get('chartName')} "
                                f"{h.get('chartVersion')} {h.get('status')}"})
        return out

    async def _flux(self, t0, namespaces: list[str]) -> list[dict]:
        ns = self.s.flux_source_namespace
        try:
            data = await self.k8s._get(f"/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/{ns}/kustomizations")
        except ToolError as exc:
            if exc.code == "not_found":
                return []
            raise
        out = []
        for ks in data.get("items", []):
            for c in ks.get("status", {}).get("conditions", []) or []:
                if c.get("type") == "Ready" and c.get("lastTransitionTime"):
                    out.append({"time": c["lastTransitionTime"], "source": "flux_kustomization",
                                "summary": f"Kustomization {ks['metadata']['name']} Ready={c['status']} "
                                f"{c.get('reason')}: {(c.get('message') or '')[:160]}"})
        events = await self.k8s._events(ns, None, None, None)
        out += [{"time": e["time"], "source": "flux_event",
                 "summary": f"{e['type']} {e['reason']} {e['object']}: {e['message'][:200]}"} for e in events]
        return out

    def tools(self) -> list[Tool]:
        return [Tool(
            "get_change_timeline",
            "One ordered list of recent changes for a service: commits, merged PRs, deploys, Terraform runs, "
            "Kubernetes rollouts and warning events, Flux reconciliations and Helm upgrades. Max 7 days, "
            "100 entries. Sources that fail are listed under gaps.",
            {"type": "object", "properties": {
                "service": {"type": "string"}, "since": {"type": "string", "description": "for example now-2h"},
                "namespace": {"type": "string"}}},
            "timeline", self.get_change_timeline, 40,
        )]
