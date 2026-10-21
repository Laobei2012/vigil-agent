"""Log tools. The model sees one interface; Loki (LogQL) or Elasticsearch serves it.

Results are summarized: the top message patterns by count plus a small sample,
not hundreds of raw lines.
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from urllib.parse import quote

import httpx

from vigil_agent.gateway.core import Tool, ToolError, ToolOutput
from vigil_agent.gateway.settings import GatewaySettings
from vigil_agent.gateway.tools.metrics import explore_url
from vigil_agent.timeutil import iso, parse_time

MAX_HITS = 200
_NORMALIZE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|0x[0-9a-fA-F]+|"
    r"\b[0-9a-f]{12,}\b|\d+(?:\.\d+)?"
)
TIME_DESC = "RFC 3339 time, 'now', or 'now-<n><s|m|h|d|w>' such as now-1h"


def pattern_of(line: str) -> str:
    return _NORMALIZE.sub("#", line)[:300]


def summarize(lines: list[tuple[str, str, dict]], sample: int = 20) -> dict:
    """lines: (timestamp, message, labels). Returns top patterns and a sample."""
    counts: Counter[str] = Counter()
    examples: dict[str, tuple[str, str]] = {}
    for ts, msg, _ in lines:
        p = pattern_of(msg)
        counts[p] += 1
        examples.setdefault(p, (ts, msg[:1000]))
    top = [
        {"count": n, "pattern": p, "example": examples[p][1], "first_seen": examples[p][0]}
        for p, n in counts.most_common(20)
    ]
    return {
        "hits": len(lines),
        "top_patterns": top,
        "sample": [{"ts": ts, "labels": labels, "message": msg[:1000]} for ts, msg, labels in lines[:sample]],
    }


class LokiLogs:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient):
        self.s = settings
        self.http = client
        self.base = (settings.loki_url or "").rstrip("/")

    def _range(self, start: str, end: str):
        t0, t1 = parse_time(start), parse_time(end)
        if t1 <= t0:
            raise ToolError("invalid_arguments", "end must be after start")
        if (t1 - t0).total_seconds() > self.s.logs_max_range_days * 86400:
            raise ToolError("invalid_arguments", f"range is longer than {self.s.logs_max_range_days} days")
        return t0, t1

    async def _get(self, path: str, params: dict) -> dict:
        resp = await self.http.get(f"{self.base}{path}", params=params, timeout=25)
        if resp.status_code >= 400:
            raise ToolError("backend_error", f"loki {resp.status_code}: {resp.text[:300]}")
        return resp.json()["data"]

    def _link(self, query: str, t0, t1) -> str:
        return explore_url(
            self.s.grafana_url, self.s.grafana_logs_uid, "loki", query,
            str(int(t0.timestamp() * 1000)), str(int(t1.timestamp() * 1000)),
        )

    def _check_selector(self, query: str) -> None:
        if not query.strip().startswith("{"):
            raise ToolError("invalid_arguments", "a LogQL query must start with a stream selector {...}")

    async def search_logs(self, query: str, start: str = "now-1h", end: str = "now",
                          size: int = 100, index_pattern: str | None = None) -> ToolOutput:
        self._check_selector(query)
        t0, t1 = self._range(start, end)
        data = await self._get(
            "/loki/api/v1/query_range",
            {"query": query, "start": int(t0.timestamp() * 1e9), "end": int(t1.timestamp() * 1e9),
             "limit": min(size, MAX_HITS), "direction": "backward"},
        )
        lines = []
        for stream in data.get("result", []):
            labels = {k: v for k, v in stream.get("stream", {}).items() if k in self.s.loki_labels}
            for ts, msg in stream.get("values", []):
                lines.append((iso_ns(ts), msg, labels))
        lines.sort(key=lambda x: x[0], reverse=True)
        out = summarize(lines)
        return ToolOutput(out, self._link(query, t0, t1), len(lines) >= min(size, MAX_HITS))

    async def logs_count(self, query: str, start: str = "now-1h", end: str = "now",
                         index_pattern: str | None = None) -> ToolOutput:
        self._check_selector(query)
        t0, t1 = self._range(start, end)
        rng = f"{max(1, int((t1 - t0).total_seconds()))}s"
        expr = f"sum(count_over_time({query} [{rng}]))"
        data = await self._get("/loki/api/v1/query", {"query": expr, "time": int(t1.timestamp() * 1e9)})
        total = 0.0
        for r in data.get("result", []):
            total += float(r["value"][1])
        return ToolOutput({"count": int(total), "start": iso(t0), "end": iso(t1)}, self._link(query, t0, t1))

    async def aggregate_terms(self, query: str, field: str, start: str = "now-1h", end: str = "now",
                              size: int = 10, index_pattern: str | None = None) -> ToolOutput:
        self._check_selector(query)
        if field not in self.s.loki_labels:
            raise ToolError("invalid_arguments", f"field must be one of {self.s.loki_labels}")
        t0, t1 = self._range(start, end)
        rng = f"{max(1, int((t1 - t0).total_seconds()))}s"
        expr = f"topk({min(size, 50)}, sum by ({field}) (count_over_time({query} [{rng}])))"
        data = await self._get("/loki/api/v1/query", {"query": expr, "time": int(t1.timestamp() * 1e9)})
        buckets = [
            {"key": r["metric"].get(field), "count": int(float(r["value"][1]))}
            for r in data.get("result", [])
        ]
        buckets.sort(key=lambda b: -b["count"])
        return ToolOutput({"field": field, "buckets": buckets}, self._link(query, t0, t1))

    def tools(self) -> list[Tool]:
        q = {"type": "string", "maxLength": 2000,
             "description": "LogQL, for example {namespace=\"x\"} |= \"error\""}
        common = {"start": {"type": "string", "description": TIME_DESC},
                  "end": {"type": "string", "description": TIME_DESC},
                  "index_pattern": {"type": "string", "description": "ignored for Loki"}}
        return _log_tools(self, q, common, "Loki (LogQL), max range 14 days")


class ElasticsearchLogs:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient):
        self.s = settings
        self.http = client
        self.base = (settings.es_url or "").rstrip("/")
        self.auth = (settings.es_username, settings.es_password) if settings.es_username else None

    def _index(self, index_pattern: str | None) -> str:
        if not index_pattern:
            raise ToolError("invalid_arguments", "index_pattern is required")
        if not any(fnmatch.fnmatch(index_pattern, allowed) for allowed in self.s.es_indices):
            raise ToolError("invalid_arguments", f"index {index_pattern} is not in the allowlist")
        return index_pattern

    def _body(self, query: str, start: str, end: str) -> dict:
        t0, t1 = parse_time(start), parse_time(end)
        if (t1 - t0).total_seconds() > 7 * 86400:
            raise ToolError("invalid_arguments", "range is longer than 7 days")
        return {
            "bool": {
                "must": [{"query_string": {"query": query}}],
                "filter": [{"range": {"@timestamp": {"gte": iso(t0), "lte": iso(t1)}}}],
            }
        }

    async def _post(self, path: str, body: dict) -> dict:
        resp = await self.http.post(f"{self.base}{path}", json=body, auth=self.auth, timeout=25)
        if resp.status_code >= 400:
            raise ToolError("backend_error", f"elasticsearch {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _link(self, index: str, query: str, start: str, end: str) -> str | None:
        if not self.s.kibana_url:
            return None
        return (f"{self.s.kibana_url.rstrip('/')}/app/discover#/?_g=(time:(from:'{iso(parse_time(start))}',"
                f"to:'{iso(parse_time(end))}'))&_a=(index:'{quote(index)}',query:(language:lucene,"
                f"query:'{quote(query)}'))")

    async def search_logs(self, query: str, start: str = "now-1h", end: str = "now",
                          size: int = 100, index_pattern: str | None = None) -> ToolOutput:
        index = self._index(index_pattern)
        body = {"query": self._body(query, start, end), "size": min(size, MAX_HITS),
                "sort": [{"@timestamp": "desc"}], "_source": self.s.es_fields}
        data = await self._post(f"/{index}/_search", body)
        lines = []
        for hit in data.get("hits", {}).get("hits", []):
            src = _flatten(hit.get("_source", {}))
            fields = {k: v for k, v in src.items() if k in self.s.es_fields}
            msg = str(fields.pop("message", "") or fields.get("error.message", ""))
            lines.append((str(fields.pop("@timestamp", "")), msg, fields))
        return ToolOutput(summarize(lines), self._link(index, query, start, end), len(lines) >= MAX_HITS)

    async def logs_count(self, query: str, start: str = "now-1h", end: str = "now",
                         index_pattern: str | None = None) -> ToolOutput:
        index = self._index(index_pattern)
        data = await self._post(f"/{index}/_count", {"query": self._body(query, start, end)})
        return ToolOutput({"count": data.get("count", 0)}, self._link(index, query, start, end))

    async def aggregate_terms(self, query: str, field: str, start: str = "now-1h", end: str = "now",
                              size: int = 10, index_pattern: str | None = None) -> ToolOutput:
        index = self._index(index_pattern)
        if field not in self.s.es_fields:
            raise ToolError("invalid_arguments", f"field must be one of {self.s.es_fields}")
        body = {"query": self._body(query, start, end), "size": 0,
                "aggs": {"terms": {"terms": {"field": field, "size": min(size, 50)}}}}
        data = await self._post(f"/{index}/_search", body)
        buckets = [{"key": b["key"], "count": b["doc_count"]}
                   for b in data.get("aggregations", {}).get("terms", {}).get("buckets", [])]
        return ToolOutput({"field": field, "buckets": buckets}, self._link(index, query, start, end))

    def tools(self) -> list[Tool]:
        q = {"type": "string", "maxLength": 2000, "description": "Lucene query string"}
        common = {"start": {"type": "string", "description": TIME_DESC},
                  "end": {"type": "string", "description": TIME_DESC},
                  "index_pattern": {"type": "string", "description": "an allowed index pattern"}}
        return _log_tools(self, q, common, "Elasticsearch (Lucene), max range 7 days")


def _log_tools(impl, q: dict, common: dict, backend_desc: str) -> list[Tool]:
    return [
        Tool(
            "search_logs",
            f"Search logs in {backend_desc}. Returns the top message patterns by count, a sample "
            "of at most 20 lines, and an evidence link. size is at most 200.",
            {"type": "object",
             "properties": {"query": q, **common, "size": {"type": "integer", "minimum": 1, "maximum": MAX_HITS}},
             "required": ["query"]},
            "logs", impl.search_logs,
        ),
        Tool(
            "logs_count",
            f"Count log lines that match a query in {backend_desc}.",
            {"type": "object", "properties": {"query": q, **common}, "required": ["query"]},
            "logs", impl.logs_count,
        ),
        Tool(
            "aggregate_terms",
            f"Count matching log lines grouped by one field or label in {backend_desc}.",
            {"type": "object",
             "properties": {"query": q, "field": {"type": "string"}, **common,
                            "size": {"type": "integer", "minimum": 1, "maximum": 50}},
             "required": ["query", "field"]},
            "logs", impl.aggregate_terms,
        ),
    ]


def iso_ns(ts: str) -> str:
    from datetime import UTC, datetime

    return iso(datetime.fromtimestamp(int(ts) / 1e9, UTC))


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out
