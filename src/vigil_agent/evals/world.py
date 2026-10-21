"""A recorded world: tool responses keyed by tool name and normalized arguments.

A call that matches a recorded entry gets its response. Otherwise the tool's
"*" entry answers, if one exists. Otherwise the call gets "no data" and is
logged, so a scenario stays reproducible and shows where its world is thin.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("vigil.evals.world")


def _key(tool: str, args: dict) -> str:
    return tool + "|" + json.dumps(args, sort_keys=True)


class World:
    def __init__(self, entries: list[dict], schemas: list[dict] | None = None):
        self.exact: dict[str, dict] = {}
        self.default: dict[str, dict] = {}
        for e in entries:
            if e.get("args") == "*":
                self.default[e["tool"]] = e["response"]
            else:
                self.exact[_key(e["tool"], e.get("args", {}))] = e["response"]
        tools = sorted({e["tool"] for e in entries})
        self._schemas = schemas or [
            {"name": t, "description": f"recorded tool {t}", "input_schema": {"type": "object"}} for t in tools
        ]
        self.calls: list[tuple[str, dict]] = []
        self.misses: list[tuple[str, dict]] = []

    @classmethod
    def load(cls, directory: str | Path) -> World:
        d = Path(directory)
        entries = json.loads((d / "world.json").read_text())
        schemas = json.loads((d / "schemas.json").read_text()) if (d / "schemas.json").exists() else None
        return cls(entries, schemas)

    async def schemas(self) -> list[dict]:
        return self._schemas

    async def call(self, name: str, args: dict, incident_id: str | None = None) -> dict:
        self.calls.append((name, args))
        response = self.exact.get(_key(name, args)) or self.default.get(name)
        if response is None:
            self.misses.append((name, args))
            log.info("unrecorded call %s %s", name, json.dumps(args)[:200])
            return {
                "ok": True,
                "data": {"note": "no data recorded for this query"},
                "evidence_url": None,
                "truncated": False,
            }
        return {"truncated": False, "evidence_url": None, **response}
