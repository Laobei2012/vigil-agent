"""Read-only PagerDuty REST client: incident alerts and incident history."""

from __future__ import annotations

import asyncio

import httpx

from vigil_agent.intake.events import Alert, pd_alert_to_alert


class PagerDutyError(Exception):
    pass


class PagerDutyClient:
    def __init__(self, api_key: str | None, base_url: str = "https://api.pagerduty.com",
                 http: httpx.AsyncClient | None = None, retries: int = 3, backoff: float = 1.0):
        self.api_key = api_key
        self.base = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=10)
        self.retries = retries
        self.backoff = backoff

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.api_key:
            raise PagerDutyError("no PagerDuty API key is configured")
        headers = {"Authorization": f"Token token={self.api_key}", "Accept": "application/json"}
        last = "no attempt"
        for attempt in range(self.retries):
            if attempt:
                await asyncio.sleep(self.backoff * 2 ** (attempt - 1))
            try:
                resp = await self.http.get(f"{self.base}{path}", params=params, headers=headers)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                continue
            if resp.status_code >= 400:
                raise PagerDutyError(f"PagerDuty {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise PagerDutyError(f"PagerDuty API failed after {self.retries} attempts: {last}")

    async def incident_alerts(self, incident_id: str) -> list[Alert]:
        data = await self._get(f"/incidents/{incident_id}/alerts", {"limit": 100})
        return [pd_alert_to_alert(a) for a in data.get("alerts", [])]

    async def past_incidents(self, service_id: str, title_hint: str, limit: int = 3) -> list[dict]:
        data = await self._get("/incidents", {"service_ids[]": service_id, "statuses[]": "resolved",
                                              "date_range": "all", "limit": 50, "sort_by": "created_at:desc"})
        words = {w for w in title_hint.lower().split() if len(w) > 3}
        scored = []
        for inc in data.get("incidents", []):
            title = (inc.get("title") or "").lower()
            score = sum(1 for w in words if w in title)
            if score:
                scored.append((score, inc))
        scored.sort(key=lambda x: -x[0])
        return [{"id": i["id"], "title": i.get("title"), "created_at": i.get("created_at"),
                 "resolve_reason": i.get("resolve_reason"), "url": i.get("html_url")}
                for _, i in scored[:limit]]
