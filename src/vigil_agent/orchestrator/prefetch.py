"""Pre-fetch: run the context pack and the standard queries in parallel, with no model.

The result feeds the fast pass, and it is also the degraded-mode reply when the
model is off, throttled, over budget, or late. A query that fails or is still
open at the deadline becomes a gap.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from vigil_agent.agent.loop import Evidence
from vigil_agent.catalog import ServiceEntry

log = logging.getLogger("vigil.prefetch")

ToolCall = Callable[[str, dict], Awaitable[dict]]
SimilarFn = Callable[[], Awaitable[list[dict]]]

DEFAULT_LOG_FILTER = '|~ "(?i)(error|fail|panic|fatal|exception|timeout|refused)"'


def _escape_label(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def plan(*, alert_name: str, labels: dict, entry: ServiceEntry | None, logs_backend: str = "loki") -> dict:
    """Decide which queries to run. Pure, so it is easy to test."""
    pack = entry.context_pack(alert_name) if entry else None
    namespace = labels.get("namespace")
    gaps = []
    if entry and namespace and namespace not in entry.namespaces:
        # The label must not move the reads to a namespace that the service does not own.
        gaps.append(f"alert namespace {namespace} is not a namespace of service {entry.name}; it was not read")
        namespace = None
        labels = {k: v for k, v in labels.items() if k != "pod"}
    namespaces = [namespace] if namespace else (entry.namespaces[:2] if entry else [])
    metric_queries = list(pack.metric_queries) if pack else []
    log_query = pack.log_query if pack else None
    if not log_query and namespace and logs_backend == "loki":
        sel = f'{{namespace="{_escape_label(namespace)}"'
        if labels.get("pod"):
            sel += f', pod="{_escape_label(labels["pod"])}"'
        log_query = sel + "} " + DEFAULT_LOG_FILTER
    runbook = (pack.runbook if pack else None) or (entry.runbook if entry else None)
    return {
        "alert_name": alert_name,
        "metric_queries": metric_queries,
        "log_query": log_query,
        "namespaces": namespaces,
        "service": entry.name if entry else None,
        "runbook": runbook,
        "log_index": entry.log_index if entry else None,
        "jenkins_job": labels.get("jenkins_job"),
        "dashboards": (pack.dashboards if pack else []) + (entry.dashboards if entry else []),
        "gaps": gaps,
    }


async def prefetch(p: dict, call: ToolCall, similar: SimilarFn, evidence: Evidence, budget_seconds: float) -> dict:
    out: dict = {
        "metrics": [],
        "logs": None,
        "timeline": {},
        "events": [],
        "similar": [],
        "alert_rule": None,
        "runbook": p["runbook"],
        "dashboards": p["dashboards"],
        "gaps": list(p.get("gaps", [])),
    }

    async def tool(name: str, args: dict) -> dict:
        res = await call(name, args)
        if not res.get("ok"):
            raise RuntimeError(f"{res.get('error_code')}: {res.get('error')}")
        return res

    def cite(name: str, args: dict, res: dict) -> str:
        return evidence.add(f"{name} {json.dumps(args)[:150]}", res.get("evidence_url"), name)

    async def alert_rule():
        res = await tool("list_alert_rules", {"name_filter": p["alert_name"]})
        rules = [r for r in res["data"].get("rules", []) if r["name"] == p["alert_name"]]
        if rules:
            rule = rules[0]
            out["alert_rule"] = rule
            out["alert_rule"]["evidence_id"] = cite("list_alert_rules", {"name": p["alert_name"]}, res)
            if rule.get("query") and not p["metric_queries"]:
                await metric(rule["query"])
            ann = rule.get("annotations", {})
            out["runbook"] = out["runbook"] or ann.get("runbook_url")

    async def metric(q: str):
        args = {"query": q, "start": "now-1h", "end": "now", "step": "60s"}
        res = await tool("query_range", args)
        out["metrics"].append(
            {
                "query": q,
                "evidence_id": cite("query_range", args, res),
                "series": res["data"].get("series", [])[:10],
                "truncated": res.get("truncated"),
            }
        )

    async def logs():
        args = {"query": p["log_query"], "start": "now-1h", "end": "now", "size": 100}
        if p["log_index"]:
            args["index_pattern"] = p["log_index"]
        res = await tool("search_logs", args)
        out["logs"] = {"query": p["log_query"], "evidence_id": cite("search_logs", args, res), **res["data"]}

    async def timeline():
        args = {"since": "now-2h"}
        if p["service"]:
            args["service"] = p["service"]
        elif p["namespaces"]:
            args["namespace"] = p["namespaces"][0]
        else:
            return
        res = await tool("get_change_timeline", args)
        out["timeline"] = {"evidence_id": cite("get_change_timeline", args, res), **res["data"]}
        out["gaps"] += [f"timeline {g}" for g in res["data"].get("gaps", [])]

    async def events(ns: str):
        args = {"namespace": ns, "type": "Warning", "since": "now-1h"}
        res = await tool("k8s_events", args)
        eid = cite("k8s_events", args, res)
        out["events"] += [dict(e, evidence_id=eid) for e in res["data"].get("events", [])[:20]]

    async def jenkins():
        args = {"job": p["jenkins_job"], "build": "lastFailedBuild", "tail_lines": 150}
        res = await tool("jenkins_build_log", args)
        out["jenkins"] = {"evidence_id": cite("jenkins_build_log", args, res), **res["data"]}

    async def similar_incidents():
        out["similar"] = await similar()

    jobs: dict[str, Awaitable] = {"alert_rule": alert_rule(), "similar": similar_incidents()}
    for q in p["metric_queries"]:
        jobs[f"metric {q[:60]}"] = metric(q)
    if p["log_query"]:
        jobs["logs"] = logs()
    if p.get("jenkins_job"):
        jobs["jenkins build log"] = jenkins()
    jobs["timeline"] = timeline()
    for ns in p["namespaces"]:
        jobs[f"events {ns}"] = events(ns)

    tasks = {name: asyncio.ensure_future(job) for name, job in jobs.items()}
    done, pending = await asyncio.wait(tasks.values(), timeout=budget_seconds)
    for name, task in tasks.items():
        if task in pending:
            task.cancel()
            out["gaps"].append(f"{name}: not finished in {budget_seconds:.0f}s")
        elif task.exception() is not None:
            out["gaps"].append(f"{name}: {task.exception()}"[:300])
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    out["evidence"] = evidence.items
    return out
