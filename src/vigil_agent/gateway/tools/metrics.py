"""Metrics tools over the Prometheus HTTP API (Prometheus or VictoriaMetrics vmselect)."""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import quote

import httpx

from vigil_agent.gateway.core import Tool, ToolError, ToolOutput
from vigil_agent.gateway.settings import GatewaySettings
from vigil_agent.timeutil import parse_time


def explore_url(grafana: str, uid: str, ds_type: str, expr: str, start: str, end: str) -> str:
    """A Grafana Explore link that reruns the exact query the agent ran."""
    query = {"refId": "A", "datasource": {"type": ds_type, "uid": uid}}
    query["expr"] = expr
    panes = {"a": {"datasource": uid, "queries": [query], "range": {"from": start, "to": end}}}
    return (
        f"{grafana.rstrip('/')}/explore?schemaVersion=1&orgId=1&panes={quote(json.dumps(panes, separators=(',', ':')))}"
    )


def _ms(dt) -> str:
    return str(int(dt.timestamp() * 1000))


class MetricsTools:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient):
        self.s = settings
        self.http = client
        self.base = (settings.metrics_url or "").rstrip("/")

    async def _get(self, path: str, params: dict) -> dict:
        resp = await self.http.get(f"{self.base}{path}", params=params, timeout=self.s.metrics_timeout_seconds)
        if resp.status_code >= 400:
            raise ToolError("backend_error", f"metrics backend {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if body.get("status") != "success":
            raise ToolError("backend_error", f"metrics query failed: {body.get('error')}")
        return body["data"]

    def _cap(self, result: list) -> tuple[list, bool]:
        if len(result) > self.s.metrics_max_series:
            return result[: self.s.metrics_max_series], True
        return result, False

    async def query_instant(self, query: str, time: str | None = None) -> ToolOutput:
        at = parse_time(time)
        data = await self._get(
            "/api/v1/query",
            {"query": query, "time": at.timestamp(), "timeout": f"{int(self.s.metrics_timeout_seconds)}s"},
        )
        result, truncated = self._cap(data.get("result", []))
        series = [{"metric": r.get("metric", {}), "value": r.get("value")} for r in result]
        url = explore_url(
            self.s.grafana_url,
            self.s.grafana_metrics_uid,
            "prometheus",
            query,
            _ms(at - timedelta(hours=1)),
            _ms(at),
        )
        return ToolOutput(
            {"result_type": data.get("resultType"), "series": series, "series_count": len(series)},
            url,
            truncated,
        )

    async def query_range(self, query: str, start: str, end: str = "now", step: str = "60s") -> ToolOutput:
        t0, t1 = parse_time(start), parse_time(end)
        if t1 <= t0:
            raise ToolError("invalid_arguments", "end must be after start")
        if (t1 - t0).total_seconds() > self.s.metrics_max_range_days * 86400:
            raise ToolError("invalid_arguments", f"range is longer than {self.s.metrics_max_range_days} days")
        step_s = _step_seconds(step)
        if step_s < self.s.metrics_min_step_seconds:
            raise ToolError("invalid_arguments", f"step must be at least {self.s.metrics_min_step_seconds}s")
        if (t1 - t0).total_seconds() / step_s > 11000:
            raise ToolError("invalid_arguments", "too many points, increase step")
        data = await self._get(
            "/api/v1/query_range",
            {
                "query": query,
                "start": t0.timestamp(),
                "end": t1.timestamp(),
                "step": step_s,
                "timeout": f"{int(self.s.metrics_timeout_seconds)}s",
            },
        )
        result, truncated = self._cap(data.get("result", []))
        series = [_summarize_series(r) for r in result]
        url = explore_url(self.s.grafana_url, self.s.grafana_metrics_uid, "prometheus", query, _ms(t0), _ms(t1))
        return ToolOutput({"series": series, "series_count": len(series)}, url, truncated)

    async def list_alert_rules(self, name_filter: str | None = None, state: str | None = None) -> ToolOutput:
        data = await self._get("/api/v1/rules", {"type": "alert"})
        rules = []
        for group in data.get("groups", []):
            for rule in group.get("rules", []):
                if name_filter and name_filter.lower() not in rule.get("name", "").lower():
                    continue
                if state and rule.get("state") != state:
                    continue
                rules.append(
                    {
                        "group": group.get("name"),
                        "name": rule.get("name"),
                        "query": rule.get("query"),
                        "duration": rule.get("duration"),
                        "labels": rule.get("labels", {}),
                        "annotations": rule.get("annotations", {}),
                        "state": rule.get("state"),
                        "active": [
                            {"labels": a.get("labels"), "state": a.get("state"), "activeAt": a.get("activeAt")}
                            for a in rule.get("alerts", [])[:10]
                        ],
                    }
                )
        truncated = len(rules) > 50
        return ToolOutput({"rules": rules[:50], "count": len(rules)}, None, truncated)

    async def series_labels(self, match: str, start: str = "now-1h", end: str = "now") -> ToolOutput:
        t0, t1 = parse_time(start), parse_time(end)
        if (t1 - t0).total_seconds() > self.s.metrics_max_range_days * 86400:
            raise ToolError("invalid_arguments", f"range is longer than {self.s.metrics_max_range_days} days")
        # limit stops a broad matcher from returning every series before the cap applies.
        params = {
            "match[]": match,
            "start": t0.timestamp(),
            "end": t1.timestamp(),
            "limit": self.s.metrics_max_series + 1,
        }
        data = await self._get("/api/v1/series", params)
        series = data if isinstance(data, list) else []
        capped, truncated = self._cap(series)
        return ToolOutput({"series": capped, "count": len(series)}, None, truncated)

    def tools(self) -> list[Tool]:
        b = "metrics"
        t = self.s.metrics_timeout_seconds + 2
        time_desc = "RFC 3339 time, 'now', or 'now-<n><s|m|h|d|w>' such as now-1h"
        return [
            Tool(
                "query_instant",
                "Run a PromQL/MetricsQL instant query. Returns at most 50 series and a Grafana link.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "maxLength": 2000},
                        "time": {"type": "string", "description": time_desc},
                    },
                    "required": ["query"],
                },
                b,
                self.query_instant,
                t,
            ),
            Tool(
                "query_range",
                "Run a PromQL/MetricsQL range query (max 7 days, step >= 15s). Each series is "
                "summarized as min, max, last, and a down-sampled list of points.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "maxLength": 2000},
                        "start": {"type": "string", "description": time_desc},
                        "end": {"type": "string", "description": time_desc},
                        "step": {"type": "string", "description": "for example 60s or 5m"},
                    },
                    "required": ["query", "start"],
                },
                b,
                self.query_range,
                t,
            ),
            Tool(
                "list_alert_rules",
                "List alerting rules with their query, duration, labels, annotations (summary, "
                "description, runbook), state, and active alerts.",
                {
                    "type": "object",
                    "properties": {
                        "name_filter": {"type": "string", "maxLength": 200},
                        "state": {"type": "string", "enum": ["inactive", "pending", "firing"]},
                    },
                },
                b,
                self.list_alert_rules,
                t,
            ),
            Tool(
                "series_labels",
                'List the label sets of series that match a selector, for example {namespace="x"}.',
                {
                    "type": "object",
                    "properties": {
                        "match": {"type": "string", "maxLength": 1000},
                        "start": {"type": "string", "description": time_desc},
                        "end": {"type": "string", "description": time_desc},
                    },
                    "required": ["match"],
                },
                b,
                self.series_labels,
                t,
            ),
        ]


def _step_seconds(step: str) -> float:
    step = step.strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if step and step[-1] in units:
        return float(step[:-1]) * units[step[-1]]
    return float(step)


def _summarize_series(r: dict, max_points: int = 30) -> dict:
    values = r.get("values", [])
    nums = []
    for _, v in values:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            continue
    stride = max(1, len(values) // max_points)
    sampled = values[::stride]
    if values and sampled[-1] != values[-1]:
        sampled.append(values[-1])
    return {
        "metric": r.get("metric", {}),
        "points": len(values),
        "min": min(nums) if nums else None,
        "max": max(nums) if nums else None,
        "first": values[0] if values else None,
        "last": values[-1] if values else None,
        "sampled": sampled,
    }
