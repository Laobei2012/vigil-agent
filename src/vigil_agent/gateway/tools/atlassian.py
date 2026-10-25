"""Jira and Confluence tools, read-only, with project and space allowlists.

Both are off when their URL is not set, as in the OCI test cluster.
"""

from __future__ import annotations

import re

import httpx

from vigil_agent.gateway.core import Tool, ToolError, ToolOutput, check_segment
from vigil_agent.gateway.settings import GatewaySettings

MAX_JQL = 500
MAX_PAGE_CHARS = 20_000
_TAGS = re.compile(r"<[^>]+>")


class JiraTools:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient):
        self.s = settings
        self.http = client
        self.base = (settings.jira_url or "").rstrip("/")
        self.auth = (settings.jira_user or "", settings.jira_token or "")

    async def _get(self, path: str, params: dict | None = None) -> dict:
        resp = await self.http.get(f"{self.base}{path}", params=params, auth=self.auth, timeout=20)
        if resp.status_code >= 400:
            raise ToolError("backend_error", f"jira {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _project_clause(self) -> str:
        return "project in (" + ",".join(f'"{p}"' for p in self.s.jira_projects) + ")"

    async def search_issues(self, jql: str, max_results: int = 10) -> ToolOutput:
        if len(jql) > MAX_JQL:
            raise ToolError("invalid_arguments", f"jql is longer than {MAX_JQL} characters")
        if not self.s.jira_projects:
            raise ToolError("not_configured", "no Jira projects are allowed")
        query = f"({jql}) AND {self._project_clause()}"
        data = await self._get(
            "/rest/api/3/search/jql",
            {
                "jql": query,
                "maxResults": min(max_results, 20),
                "fields": "summary,status,resolution,created,resolutiondate,labels,components,priority",
            },
        )
        issues = [
            {
                "key": i["key"],
                **{k: _field(v) for k, v in i.get("fields", {}).items()},
                "url": f"{self.base}/browse/{i['key']}",
            }
            for i in data.get("issues", [])
        ]
        return ToolOutput({"issues": issues}, f"{self.base}/issues/?jql={query}")

    async def get_issue(self, key: str) -> ToolOutput:
        check_segment("jira_key", key, "key")
        if key.split("-", 1)[0] not in self.s.jira_projects:
            raise ToolError("forbidden", f"project of {key} is not in the allowlist")
        data = await self._get(f"/rest/api/3/issue/{key}", {"expand": "renderedFields"})
        rendered = data.get("renderedFields", {}) or {}
        fields = data.get("fields", {}) or {}
        comments = (rendered.get("comment") or {}).get("comments", [])[-10:]
        return ToolOutput(
            {
                "key": key,
                "summary": fields.get("summary"),
                "status": _field(fields.get("status")),
                "resolution": _field(fields.get("resolution")),
                "description": _TAGS.sub("", rendered.get("description") or "")[:8000],
                "comments": [_TAGS.sub("", c.get("body", ""))[:2000] for c in comments],
            },
            f"{self.base}/browse/{key}",
        )

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "search_issues",
                "Search Jira issues (postmortems, past incidents) with JQL. Results are "
                "limited to the allowed projects.",
                {
                    "type": "object",
                    "properties": {
                        "jql": {"type": "string", "maxLength": MAX_JQL},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["jql"],
                },
                "jira",
                self.search_issues,
            ),
            Tool(
                "get_issue",
                "Read one Jira issue with its description and last comments.",
                {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
                "jira",
                self.get_issue,
            ),
        ]


class ConfluenceTools:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient):
        self.s = settings
        self.http = client
        self.base = (settings.confluence_url or "").rstrip("/")
        self.auth = (settings.confluence_user or "", settings.confluence_token or "")

    async def _get(self, path: str, params: dict | None = None) -> dict:
        resp = await self.http.get(f"{self.base}{path}", params=params, auth=self.auth, timeout=20)
        if resp.status_code >= 400:
            raise ToolError("backend_error", f"confluence {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def confluence_search(self, cql: str, limit: int = 10) -> ToolOutput:
        if not self.s.confluence_spaces:
            raise ToolError("not_configured", "no Confluence spaces are allowed")
        spaces = ",".join(f'"{s}"' for s in self.s.confluence_spaces)
        query = f"({cql}) AND space in ({spaces})"
        data = await self._get("/rest/api/content/search", {"cql": query, "limit": min(limit, 20)})
        return ToolOutput(
            {
                "results": [
                    {
                        "id": r["id"],
                        "title": r.get("title"),
                        "url": f"{self.base}{r.get('_links', {}).get('webui', '')}",
                    }
                    for r in data.get("results", [])
                ]
            }
        )

    async def get_page(self, page_id: str) -> ToolOutput:
        check_segment("confluence_id", page_id, "page_id")
        data = await self._get(f"/rest/api/content/{page_id}", {"expand": "body.storage,space"})
        space = (data.get("space") or {}).get("key")
        if space not in self.s.confluence_spaces:
            raise ToolError("forbidden", f"space {space} is not in the allowlist")
        body = _TAGS.sub(" ", data.get("body", {}).get("storage", {}).get("value", ""))
        return ToolOutput(
            {"id": page_id, "title": data.get("title"), "space": space, "body": body[:MAX_PAGE_CHARS]},
            f"{self.base}{data.get('_links', {}).get('webui', '')}",
            len(body) > MAX_PAGE_CHARS,
        )

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "confluence_search",
                "Search runbook and architecture pages in Confluence with CQL.",
                {
                    "type": "object",
                    "properties": {
                        "cql": {"type": "string", "maxLength": 500},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["cql"],
                },
                "confluence",
                self.confluence_search,
            ),
            Tool(
                "get_page",
                "Read one Confluence page as text, truncated to 20000 characters.",
                {"type": "object", "properties": {"page_id": {"type": "string"}}, "required": ["page_id"]},
                "confluence",
                self.get_page,
            ),
        ]


def _field(v):
    if isinstance(v, dict):
        return v.get("name") or v.get("value") or v.get("key")
    if isinstance(v, list):
        return [_field(x) for x in v]
    return v
