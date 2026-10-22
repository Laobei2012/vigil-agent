"""Read-only Jenkins tools: recent builds of a job, and the console log of one build.

The credential is a Jenkins user with only Overall/Read and Job/Read. Jobs
outside the allowlist are refused in the tool code as well.
"""

from __future__ import annotations

import fnmatch
import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from vigil_agent.gateway.core import Tool, ToolError, ToolOutput
from vigil_agent.gateway.settings import GatewaySettings

MAX_TAIL = 500
MAX_ERROR_LINES = 50
MAX_LOG_BYTES = 5_000_000
_BUILD = re.compile(r"^(\d+|lastBuild|lastFailedBuild|lastSuccessfulBuild|lastUnsuccessfulBuild|lastCompletedBuild)$")
_ERROR = re.compile(
    r"(?i)\b(error|fail(ed|ure)?|exception|traceback|fatal|exit code [1-9]|not found|denied|timed? ?out)\b"
)


def _iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")


class JenkinsTools:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient):
        self.s = settings
        self.http = client
        self.base = (settings.jenkins_url or "").rstrip("/")
        self.public = (settings.jenkins_public_url or settings.jenkins_url or "").rstrip("/")
        self.auth = (settings.jenkins_user or "", settings.jenkins_token or "")

    def _job_path(self, job: str) -> str:
        parts = [p for p in job.strip("/").split("/") if p]
        if not parts or any(p in (".", "..") for p in parts):
            raise ToolError("invalid_arguments", "job must be a job name, or folder/job for a job in a folder")
        if not any(fnmatch.fnmatch(job.strip("/"), pattern) for pattern in self.s.jenkins_jobs):
            raise ToolError("forbidden", f"job {job} is not in the allowlist")
        return "".join(f"/job/{quote(p, safe='')}" for p in parts)

    async def _get(self, path: str, params: dict | None = None, text: bool = False):
        resp = await self.http.get(f"{self.base}{path}", params=params, auth=self.auth, timeout=25)
        if resp.status_code == 404:
            raise ToolError("not_found", f"not found in Jenkins: {path}")
        if resp.status_code >= 400:
            raise ToolError("backend_error", f"jenkins {resp.status_code}: {resp.text[:200]}")
        return resp.text if text else resp.json()

    async def jenkins_job_status(self, job: str) -> ToolOutput:
        path = self._job_path(job)
        tree = (
            "builds[number,result,timestamp,duration,building,builtOn]{0,10},"
            "lastSuccessfulBuild[number],lastFailedBuild[number],inQueue"
        )
        data = await self._get(f"{path}/api/json", {"tree": tree})
        builds = [
            {
                "number": b.get("number"),
                "result": b.get("result") or ("BUILDING" if b.get("building") else None),
                "started": _iso(b.get("timestamp")),
                "duration_s": round((b.get("duration") or 0) / 1000),
                "agent": b.get("builtOn") or "built-in",
            }
            for b in data.get("builds", [])
        ]
        return ToolOutput(
            {
                "job": job,
                "builds": builds,
                "in_queue": data.get("inQueue"),
                "last_successful": (data.get("lastSuccessfulBuild") or {}).get("number"),
                "last_failed": (data.get("lastFailedBuild") or {}).get("number"),
            },
            f"{self.public}{path}/",
        )

    async def jenkins_build_log(self, job: str, build: str = "lastFailedBuild", tail_lines: int = 200) -> ToolOutput:
        if not _BUILD.match(build):
            raise ToolError("invalid_arguments", "build must be a number or lastFailedBuild, lastBuild, ...")
        if tail_lines > MAX_TAIL:
            raise ToolError("invalid_arguments", f"tail_lines must be at most {MAX_TAIL}")
        path = f"{self._job_path(job)}/{build}"
        info = await self._get(
            f"{path}/api/json", {"tree": "number,result,timestamp,duration,builtOn,actions[causes[shortDescription]]"}
        )
        text = await self._get(f"{path}/consoleText", text=True)
        truncated = len(text) > MAX_LOG_BYTES
        lines = text[-MAX_LOG_BYTES:].splitlines()
        errors = [{"line": i + 1, "text": ln[:400]} for i, ln in enumerate(lines) if _ERROR.search(ln)]
        causes = [
            c.get("shortDescription") for a in info.get("actions", []) or [] if a for c in a.get("causes", []) or []
        ]
        number = info.get("number")
        return ToolOutput(
            {
                "job": job,
                "build": number,
                "result": info.get("result"),
                "started": _iso(info.get("timestamp")),
                "duration_s": round((info.get("duration") or 0) / 1000),
                "agent": info.get("builtOn") or "built-in",
                "causes": causes,
                "total_lines": len(lines),
                "error_lines": errors[-MAX_ERROR_LINES:],
                "tail": [ln[:400] for ln in lines[-tail_lines:]],
            },
            f"{self.public}{self._job_path(job)}/{number or build}/console",
            truncated or len(lines) > tail_lines,
        )

    def tools(self) -> list[Tool]:
        job = {"type": "string", "description": "job name, or folder/job"}
        return [
            Tool(
                "jenkins_job_status",
                "List the last 10 builds of a Jenkins job with result, start time, duration, "
                "and agent, plus the last successful and last failed build numbers.",
                {"type": "object", "properties": {"job": job}, "required": ["job"]},
                "jenkins",
                self.jenkins_job_status,
            ),
            Tool(
                "jenkins_build_log",
                "Read the console log of one Jenkins build: the last lines (at most 500) and "
                "every line that looks like an error, with line numbers. build is a number or lastFailedBuild.",
                {
                    "type": "object",
                    "properties": {
                        "job": job,
                        "build": {"type": "string"},
                        "tail_lines": {"type": "integer", "minimum": 1, "maximum": MAX_TAIL},
                    },
                    "required": ["job"],
                },
                "jenkins",
                self.jenkins_build_log,
            ),
        ]
