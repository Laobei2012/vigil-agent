"""Read-only Kubernetes tools over the API server, with a namespace allowlist.

The ServiceAccount has only get, list, and watch, and no access to secrets,
pods/exec, pods/attach, or pods/portforward. The tool code refuses those
resources as well, so a wrong ClusterRole is not the only control.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx

from vigil_agent.gateway.core import Tool, ToolError, ToolOutput
from vigil_agent.gateway.redaction import MASK, SECRET_KEY_PATTERN
from vigil_agent.gateway.settings import GatewaySettings
from vigil_agent.timeutil import parse_time, utcnow

# resource -> (api prefix, plural, namespaced)
RESOURCES: dict[str, tuple[str, str, bool]] = {
    "pods": ("/api/v1", "pods", True),
    "services": ("/api/v1", "services", True),
    "endpoints": ("/api/v1", "endpoints", True),
    "configmaps": ("/api/v1", "configmaps", True),
    "events": ("/api/v1", "events", True),
    "persistentvolumeclaims": ("/api/v1", "persistentvolumeclaims", True),
    "serviceaccounts": ("/api/v1", "serviceaccounts", True),
    "nodes": ("/api/v1", "nodes", False),
    "namespaces": ("/api/v1", "namespaces", False),
    "persistentvolumes": ("/api/v1", "persistentvolumes", False),
    "deployments": ("/apis/apps/v1", "deployments", True),
    "statefulsets": ("/apis/apps/v1", "statefulsets", True),
    "daemonsets": ("/apis/apps/v1", "daemonsets", True),
    "replicasets": ("/apis/apps/v1", "replicasets", True),
    "jobs": ("/apis/batch/v1", "jobs", True),
    "cronjobs": ("/apis/batch/v1", "cronjobs", True),
    "horizontalpodautoscalers": ("/apis/autoscaling/v2", "horizontalpodautoscalers", True),
    "ingresses": ("/apis/networking.k8s.io/v1", "ingresses", True),
    "networkpolicies": ("/apis/networking.k8s.io/v1", "networkpolicies", True),
    "poddisruptionbudgets": ("/apis/policy/v1", "poddisruptionbudgets", True),
    "storageclasses": ("/apis/storage.k8s.io/v1", "storageclasses", False),
    "certificatesigningrequests": ("/apis/certificates.k8s.io/v1", "certificatesigningrequests", False),
    "httproutes": ("/apis/gateway.networking.k8s.io/v1", "httproutes", True),
    "gateways": ("/apis/gateway.networking.k8s.io/v1", "gateways", True),
    "certificates.cert-manager.io": ("/apis/cert-manager.io/v1", "certificates", True),
    "issuers.cert-manager.io": ("/apis/cert-manager.io/v1", "issuers", True),
    "clusterissuers.cert-manager.io": ("/apis/cert-manager.io/v1", "clusterissuers", False),
    "challenges.acme.cert-manager.io": ("/apis/acme.cert-manager.io/v1", "challenges", True),
    "orders.acme.cert-manager.io": ("/apis/acme.cert-manager.io/v1", "orders", True),
    "externalsecrets": ("/apis/external-secrets.io/v1", "externalsecrets", True),
    "clustersecretstores": ("/apis/external-secrets.io/v1", "clustersecretstores", False),
    "kustomizations": ("/apis/kustomize.toolkit.fluxcd.io/v1", "kustomizations", True),
    "helmreleases": ("/apis/helm.toolkit.fluxcd.io/v2", "helmreleases", True),
    "gitrepositories": ("/apis/source.toolkit.fluxcd.io/v1", "gitrepositories", True),
    "helmrepositories": ("/apis/source.toolkit.fluxcd.io/v1", "helmrepositories", True),
    "volumes.longhorn.io": ("/apis/longhorn.io/v1beta2", "volumes", True),
    "nodes.longhorn.io": ("/apis/longhorn.io/v1beta2", "nodes", True),
    "replicas.longhorn.io": ("/apis/longhorn.io/v1beta2", "replicas", True),
    "clusters.postgresql.cnpg.io": ("/apis/postgresql.cnpg.io/v1", "clusters", True),
    "prometheusrules": ("/apis/monitoring.coreos.com/v1", "prometheusrules", True),
}

DENIED = {"secrets", "pods/exec", "pods/attach", "pods/portforward", "serviceaccounts/token",
          "secret", "sealedsecrets"}
MAX_ITEMS = 100
MAX_TAIL = 500


class KubernetesTools:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.base = (settings.k8s_api_url or "https://kubernetes.default.svc").rstrip("/")
        if client is None:
            verify = settings.k8s_ca_path if Path(settings.k8s_ca_path).exists() else True
            client = httpx.AsyncClient(verify=verify, timeout=25)
        self.http = client

    def _headers(self) -> dict:
        p = Path(self.s.k8s_token_path)
        return {"Authorization": f"Bearer {p.read_text().strip()}"} if p.exists() else {}

    def _resource(self, resource: str) -> tuple[str, str, bool]:
        r = resource.strip().lower()
        if r in DENIED or r.startswith("secrets"):
            raise ToolError("forbidden", f"resource {resource} is not readable by this agent")
        if r not in RESOURCES:
            raise ToolError("invalid_arguments", f"unknown resource {resource}; use one of {sorted(RESOURCES)}")
        return RESOURCES[r]

    def _check_ns(self, namespace: str | None, namespaced: bool) -> None:
        if not namespaced:
            return
        if not namespace:
            raise ToolError("invalid_arguments", "namespace is required for this resource")
        if self.s.namespaces and namespace not in self.s.namespaces:
            raise ToolError("forbidden", f"namespace {namespace} is not in the allowlist")

    def _path(self, resource: str, namespace: str | None, name: str | None = None) -> str:
        prefix, plural, namespaced = self._resource(resource)
        self._check_ns(namespace, namespaced)
        ns = f"/namespaces/{namespace}" if namespaced else ""
        return f"{prefix}{ns}/{plural}" + (f"/{name}" if name else "")

    async def _get(self, path: str, params: dict | None = None, text: bool = False):
        resp = await self.http.get(f"{self.base}{path}", params=params, headers=self._headers())
        if resp.status_code == 404:
            raise ToolError("not_found", f"not found: {path}")
        if resp.status_code >= 400:
            raise ToolError("backend_error", f"kubernetes {resp.status_code}: {resp.text[:300]}")
        return resp.text if text else resp.json()

    async def k8s_get(self, resource: str, name: str, namespace: str | None = None) -> ToolOutput:
        obj = await self._get(self._path(resource, namespace, name))
        return ToolOutput(clean(obj))

    async def k8s_list(self, resource: str, namespace: str | None = None,
                       label_selector: str | None = None, field_selector: str | None = None) -> ToolOutput:
        params = {"limit": MAX_ITEMS}
        if label_selector:
            params["labelSelector"] = label_selector
        if field_selector:
            params["fieldSelector"] = field_selector
        data = await self._get(self._path(resource, namespace), params)
        items = data.get("items", [])
        truncated = bool(data.get("metadata", {}).get("continue"))
        return ToolOutput({"resource": resource, "count": len(items),
                           "items": [summarize_object(resource, o) for o in items]}, None, truncated)

    async def k8s_describe(self, resource: str, name: str, namespace: str | None = None) -> ToolOutput:
        obj = await self._get(self._path(resource, namespace, name))
        events = await self._events(namespace, name, None, None) if namespace else []
        return ToolOutput({"object": clean(obj), "events": events})

    async def _events(self, namespace: str | None, involved_name: str | None,
                      event_type: str | None, since: str | None) -> list[dict]:
        self._check_ns(namespace, True)
        params = {"limit": 500}
        selectors = []
        if involved_name:
            selectors.append(f"involvedObject.name={involved_name}")
        if event_type:
            selectors.append(f"type={event_type}")
        if selectors:
            params["fieldSelector"] = ",".join(selectors)
        data = await self._get(f"/api/v1/namespaces/{namespace}/events", params)
        cutoff = parse_time(since) if since else None
        events = []
        for e in data.get("items", []):
            ts = e.get("lastTimestamp") or e.get("eventTime") or e.get("metadata", {}).get("creationTimestamp")
            if cutoff and ts and _parse_k8s(ts) < cutoff:
                continue
            obj = e.get("involvedObject", {})
            events.append({
                "time": ts, "type": e.get("type"), "reason": e.get("reason"),
                "object": f"{obj.get('kind')}/{obj.get('name')}", "message": (e.get("message") or "")[:500],
                "count": e.get("count") or 1,
            })
        events.sort(key=lambda e: e["time"] or "", reverse=True)
        return events[:MAX_ITEMS]

    async def k8s_events(self, namespace: str, involved_name: str | None = None,
                         type: str | None = None, since: str | None = "now-1h") -> ToolOutput:
        events = await self._events(namespace, involved_name, type, since)
        return ToolOutput({"namespace": namespace, "count": len(events), "events": events})

    async def k8s_pod_logs(self, namespace: str, pod: str, container: str | None = None,
                           tail: int = 200, previous: bool = False) -> ToolOutput:
        if tail > MAX_TAIL:
            raise ToolError("invalid_arguments", f"tail must be at most {MAX_TAIL}")
        self._check_ns(namespace, True)
        params = {"tailLines": tail, "timestamps": "true"}
        if container:
            params["container"] = container
        if previous:
            params["previous"] = "true"
        text = await self._get(f"/api/v1/namespaces/{namespace}/pods/{pod}/log", params, text=True)
        lines = text.splitlines()[-tail:]
        return ToolOutput({"pod": pod, "container": container, "lines": [ln[:1000] for ln in lines]})

    async def k8s_top(self, namespace: str | None = None) -> ToolOutput:
        if namespace:
            self._check_ns(namespace, True)
            data = await self._get(f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods")
            rows = [{"pod": i["metadata"]["name"],
                     "containers": [{"name": c["name"], **c.get("usage", {})} for c in i.get("containers", [])]}
                    for i in data.get("items", [])]
        else:
            data = await self._get("/apis/metrics.k8s.io/v1beta1/nodes")
            rows = [{"node": i["metadata"]["name"], **i.get("usage", {})} for i in data.get("items", [])]
        return ToolOutput({"items": rows[:MAX_ITEMS]}, None, len(rows) > MAX_ITEMS)

    def tools(self) -> list[Tool]:
        res = {"type": "string", "description": "Resource, for example pods, deployments, nodes, "
               "helmreleases, kustomizations, certificates.cert-manager.io, volumes.longhorn.io, "
               "clusters.postgresql.cnpg.io, certificatesigningrequests. Secrets are not readable."}
        ns = {"type": "string"}
        b = "kubernetes"
        return [
            Tool("k8s_get", "Get one Kubernetes object (managed fields removed, ConfigMap secrets masked).",
                 {"type": "object", "properties": {"resource": res, "name": {"type": "string"}, "namespace": ns},
                  "required": ["resource", "name"]}, b, self.k8s_get),
            Tool("k8s_list", "List Kubernetes objects as summary rows (status, readiness, restarts, images). "
                 "At most 100 items.",
                 {"type": "object", "properties": {"resource": res, "namespace": ns,
                                                   "label_selector": {"type": "string"},
                                                   "field_selector": {"type": "string"}},
                  "required": ["resource"]}, b, self.k8s_list),
            Tool("k8s_describe", "Get one object together with its recent events.",
                 {"type": "object", "properties": {"resource": res, "name": {"type": "string"}, "namespace": ns},
                  "required": ["resource", "name"]}, b, self.k8s_describe),
            Tool("k8s_events", "List events of a namespace, newest first. type is Normal or Warning.",
                 {"type": "object", "properties": {"namespace": ns, "involved_name": {"type": "string"},
                                                   "type": {"type": "string", "enum": ["Normal", "Warning"]},
                                                   "since": {"type": "string", "description": "for example now-2h"}},
                  "required": ["namespace"]}, b, self.k8s_events),
            Tool("k8s_pod_logs", "Read the last lines of a container log (tail at most 500). Set previous "
                 "to read the log of the last terminated container.",
                 {"type": "object", "properties": {"namespace": ns, "pod": {"type": "string"},
                                                   "container": {"type": "string"},
                                                   "tail": {"type": "integer", "minimum": 1, "maximum": MAX_TAIL},
                                                   "previous": {"type": "boolean"}},
                  "required": ["namespace", "pod"]}, b, self.k8s_pod_logs),
            Tool("k8s_top", "CPU and memory use of the pods of a namespace, or of all nodes when namespace "
                 "is empty.", {"type": "object", "properties": {"namespace": ns}}, b, self.k8s_top),
        ]


def _parse_k8s(ts: str) -> datetime:
    return parse_time(ts)


def clean(obj: dict) -> dict:
    """Remove noise and mask ConfigMap values whose key looks like a secret."""
    obj = dict(obj)
    meta = dict(obj.get("metadata", {}))
    meta.pop("managedFields", None)
    ann = {k: v for k, v in (meta.get("annotations") or {}).items()
           if k != "kubectl.kubernetes.io/last-applied-configuration"}
    if ann:
        meta["annotations"] = ann
    else:
        meta.pop("annotations", None)
    obj["metadata"] = meta
    if obj.get("kind") == "ConfigMap":
        for field in ("data", "binaryData"):
            if isinstance(obj.get(field), dict):
                obj[field] = {k: (MASK if SECRET_KEY_PATTERN.search(k) or field == "binaryData" else v)
                              for k, v in obj[field].items()}
    return obj


def _age(ts: str | None) -> str | None:
    if not ts:
        return None
    secs = int((utcnow() - parse_time(ts)).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit}"
    return f"{secs}s"


def _conditions(status: dict) -> dict:
    return {c.get("type"): c.get("status") + (f" ({c.get('reason')})" if c.get("reason") else "")
            for c in status.get("conditions", []) or [] if c.get("type")}


def summarize_object(resource: str, o: dict) -> dict:
    meta = o.get("metadata", {})
    spec = o.get("spec", {}) or {}
    status = o.get("status", {}) or {}
    row = {"name": meta.get("name"), "namespace": meta.get("namespace"),
           "age": _age(meta.get("creationTimestamp"))}
    if resource == "pods":
        cs = status.get("containerStatuses", []) or []
        row.update({
            "phase": status.get("phase"), "node": spec.get("nodeName"),
            "ready": f"{sum(1 for c in cs if c.get('ready'))}/{len(cs)}",
            "restarts": sum(c.get("restartCount", 0) for c in cs),
            "reasons": [r for c in cs for r in (
                (c.get("state", {}).get("waiting") or {}).get("reason"),
                (c.get("lastState", {}).get("terminated") or {}).get("reason")) if r],
        })
    elif resource in ("deployments", "statefulsets", "replicasets"):
        containers = spec.get("template", {}).get("spec", {}).get("containers", [])
        row.update({"replicas": spec.get("replicas"), "ready": status.get("readyReplicas", 0),
                    "updated": status.get("updatedReplicas"), "available": status.get("availableReplicas"),
                    "images": [c.get("image") for c in containers],
                    "revision": (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision")})
    elif resource == "daemonsets":
        row.update({"desired": status.get("desiredNumberScheduled"), "ready": status.get("numberReady"),
                    "unavailable": status.get("numberUnavailable")})
    elif resource == "nodes":
        labels = meta.get("labels", {})
        row.update({"conditions": _conditions(status), "unschedulable": spec.get("unschedulable", False),
                    "labels": {k: v for k, v in labels.items()
                               if k.startswith(("topology.", "tenancy", "node.kubernetes.io/instance-type",
                                                "kubernetes.io/arch"))},
                    "taints": spec.get("taints")})
    elif resource == "certificatesigningrequests":
        row.update({"signer": spec.get("signerName"), "user": spec.get("username"),
                    "conditions": _conditions(status) or "Pending"})
    elif resource == "events":
        row.update({"type": o.get("type"), "reason": o.get("reason"), "message": (o.get("message") or "")[:300]})
    else:
        row["conditions"] = _conditions(status)
        for key in ("phase", "state", "robustness", "lastAppliedRevision", "lastAttemptedRevision"):
            if key in status:
                row[key] = status[key]
    return row
