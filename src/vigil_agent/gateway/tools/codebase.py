"""grep_code and read_file, pinned to the deployed commit.

Two backends fill a local cache directory per commit:

- "flux": the Flux source-controller artifact of the GitOps repository. It
  holds only the revision that Flux applied, so it needs no credential.
- "git": partial clones (--filter=blob:none) of allowed repositories, read
  with a read-only token; one commit is exported with git archive.

The model never gets a shell or raw git. Searches run ripgrep with a timeout.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import tarfile
from pathlib import Path

import httpx

from vigil_agent.gateway.core import Tool, ToolError, ToolOutput
from vigil_agent.gateway.settings import GatewaySettings
from vigil_agent.gateway.tools.kubernetes import KubernetesTools

MAX_MATCHES = 50
MAX_LINES = 400
MAX_FILE_BYTES = 1_000_000
GREP_TIMEOUT = 10.0
KEEP_COMMITS = 5
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class CodebaseTools:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient, k8s: KubernetesTools | None):
        self.s = settings
        self.http = client
        self.k8s = k8s
        self.cache = Path(settings.codebase_cache_dir)
        self._locks: dict[str, asyncio.Lock] = {}

    def _repo(self, repo: str | None) -> str:
        if self.s.codebase_backend == "flux":
            if repo and repo != self.s.flux_repo:
                raise ToolError("forbidden", f"repository {repo} is not available, use {self.s.flux_repo}")
            return self.s.flux_repo
        allowed = self.s.repos
        if not repo:
            if len(allowed) == 1:
                return allowed[0]
            raise ToolError("invalid_arguments", f"repo is required, one of {allowed}")
        if repo not in allowed:
            raise ToolError("forbidden", f"repository {repo} is not in the allowlist")
        return repo

    async def checkout(self, repo: str, sha: str) -> Path:
        if not _SHA.match(sha):
            raise ToolError("invalid_arguments", "sha must be a hex commit id")
        target = self.cache / repo.replace("/", "__") / sha
        if target.exists():
            return target
        lock = self._locks.setdefault(f"{repo}@{sha}", asyncio.Lock())
        async with lock:
            if target.exists():
                return target
            tmp = target.with_name(sha + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            try:
                if self.s.codebase_backend == "flux":
                    await self._fetch_flux(sha, tmp)
                else:
                    await self._fetch_git(repo, sha, tmp)
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            tmp.rename(target)
        self._prune(repo, keep=target)
        return target

    def _prune(self, repo: str, keep: Path) -> None:
        """Keep the newest KEEP_COMMITS commit directories of a repository.

        The cache is an emptyDir with a size limit, and kubelet evicts the pod when it fills up.
        """
        root = self.cache / repo.replace("/", "__")
        dirs = [d for d in root.iterdir() if d.is_dir() and _SHA.match(d.name)]
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for old in dirs[KEEP_COMMITS:]:
            if old == keep:
                continue
            shutil.rmtree(old, ignore_errors=True)
            self._locks.pop(f"{repo}@{old.name}", None)

    async def _fetch_flux(self, sha: str, dest: Path) -> None:
        repo = await self.k8s._get(
            f"/apis/source.toolkit.fluxcd.io/v1/namespaces/{self.s.flux_source_namespace}"
            f"/gitrepositories/{self.s.flux_source_name}"
        )
        artifact = repo.get("status", {}).get("artifact") or {}
        available = artifact.get("revision", "").rsplit(":", 1)[-1]
        if not available or not (available.startswith(sha) or sha.startswith(available)):
            raise ToolError(
                "revision_unavailable", f"only the revision that Flux holds ({available[:12]}) can be read here"
            )
        resp = await self.http.get(artifact["url"], timeout=30)
        if resp.status_code >= 400:
            raise ToolError("backend_error", f"flux artifact {resp.status_code}")
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            tar.extractall(dest, filter="data")

    async def _git(self, *args: str, cwd: Path | None = None) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        out, err = await asyncio.wait_for(proc.communicate(), 120)
        if proc.returncode != 0:
            raise ToolError("backend_error", f"git {args[0]} failed: {err.decode()[-300:]}")
        return out

    async def _fetch_git(self, repo: str, sha: str, dest: Path) -> None:
        mirror = self.cache / repo.replace("/", "__") / "_git"
        url = self.s.git_url_template.format(token=self.s.github_token or "", repo=repo)
        if not mirror.exists():
            await self._git("clone", "--bare", "--filter=blob:none", url, str(mirror))
        try:
            await self._git("cat-file", "-e", f"{sha}^{{commit}}", cwd=mirror)
        except ToolError:
            await self._git("fetch", "--filter=blob:none", url, sha, cwd=mirror)
        data = await self._git("archive", "--format=tar", sha, cwd=mirror)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            tar.extractall(dest, filter="data")

    async def grep_code(
        self, pattern: str, sha: str, repo: str | None = None, path_glob: str | None = None
    ) -> ToolOutput:
        repo = self._repo(repo)
        root = await self.checkout(repo, sha)
        args = ["rg", "--json", "--max-count", "5", "--max-columns", "300", "--regex-size-limit", "1M", "-e", pattern]
        if path_glob:
            args += ["--glob", path_glob]
        args.append(".")
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), GREP_TIMEOUT)
        except TimeoutError as exc:
            proc.kill()
            raise ToolError("timeout", "the search did not finish in time, use a simpler pattern") from exc
        if proc.returncode not in (0, 1):
            raise ToolError("invalid_arguments", f"search failed: {err.decode()[:300]}")
        matches = []
        for line in out.decode(errors="replace").splitlines():
            ev = json.loads(line)
            if ev.get("type") != "match":
                continue
            d = ev["data"]
            matches.append(
                {
                    "path": d["path"]["text"].removeprefix("./"),
                    "line": d["line_number"],
                    "text": d["lines"].get("text", "").rstrip()[:300],
                }
            )
        url = f"https://github.com/{repo}/search?q={pattern}"
        return ToolOutput(
            {"repo": repo, "sha": sha, "matches": matches[:MAX_MATCHES], "count": len(matches)},
            url,
            len(matches) > MAX_MATCHES,
        )

    async def read_file(
        self, path: str, sha: str, repo: str | None = None, start_line: int = 1, end_line: int | None = None
    ) -> ToolOutput:
        repo = self._repo(repo)
        if path.startswith("/") or ".." in Path(path).parts:
            raise ToolError("invalid_arguments", "path must be relative to the repository root, without ..")
        root = await self.checkout(repo, sha)
        target = (root / path).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            raise ToolError("not_found", f"no file {path} at {sha[:12]}")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ToolError("invalid_arguments", "file is too large")
        lines = target.read_text(errors="replace").splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line or start + MAX_LINES - 1, start + MAX_LINES - 1)
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        return ToolOutput(
            {
                "repo": repo,
                "sha": sha,
                "path": path,
                "start_line": start,
                "end_line": end,
                "total_lines": len(lines),
                "content": body,
            },
            f"https://github.com/{repo}/blob/{sha}/{path}#L{start}-L{end}",
            end < len(lines) and (end_line is None or end < end_line),
        )

    def tools(self) -> list[Tool]:
        repo = {"type": "string", "description": "repository; optional when only one is allowed"}
        sha = {"type": "string", "description": "the deployed commit from resolve_deployed_sha"}
        return [
            Tool(
                "grep_code",
                "Search the code at one commit with a regular expression (ripgrep). At most "
                "50 matches. Runbooks in Markdown are searchable too.",
                {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "maxLength": 300},
                        "sha": sha,
                        "repo": repo,
                        "path_glob": {"type": "string"},
                    },
                    "required": ["pattern", "sha"],
                },
                "codebase",
                self.grep_code,
                60,
            ),
            Tool(
                "read_file",
                "Read at most 400 lines of one file at one commit, with line numbers.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "sha": sha,
                        "repo": repo,
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path", "sha"],
                },
                "codebase",
                self.read_file,
                60,
            ),
        ]
