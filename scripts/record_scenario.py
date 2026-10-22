"""Turn a stored investigation into a replay scenario: alert, recorded world, and a scenario draft.

Step 1 runs in the orchestrator pod, which has the database and the gateway:

    kubectl -n vigil-agent exec -i deploy/vigil-agent-orchestrator -- \
        python - dump 12 < scripts/record_scenario.py > bundle.json

Step 2 runs on your machine and writes the files:

    python scripts/record_scenario.py write jenkins-tts-missing-script <eval-dir> < bundle.json

The world answers each recorded call exactly. The first response of each tool
also answers calls with other arguments, so a replay that asks slightly
differently still gets data. Investigations from before 2026-10-22 have no
pre-fetch results in the transcript, so their pre-fetch queries get "no data".
Review the scenario draft: the expectations come from the deep report and are
a starting point only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


async def dump(inv_id: int) -> None:
    import httpx
    import psycopg
    from psycopg.rows import dict_row

    async with await psycopg.AsyncConnection.connect(os.environ["VIGIL_DATABASE_URL"], row_factory=dict_row) as db:
        cur = await db.execute(
            "SELECT id, service, alert_name, context, deep_report, fast_report, created_at FROM investigations"
            " WHERE id = %s",
            (inv_id,),
        )
        inv = await cur.fetchone()
        if inv is None:
            sys.exit(f"investigation {inv_id} not found")
        cur = await db.execute(
            "SELECT pass, payload FROM transcripts WHERE investigation_id = %s AND kind = 'tool_call' ORDER BY id",
            (inv_id,),
        )
        calls = [{"pass": r["pass"], **r["payload"]} for r in await cur.fetchall()]
    headers = {"Authorization": f"Bearer {os.environ['VIGIL_GATEWAY_TOKEN']}"}
    async with httpx.AsyncClient(base_url=os.environ["VIGIL_GATEWAY_URL"], headers=headers, timeout=30) as gw:
        schemas = (await gw.get("/v1/tools")).json()
    bundle = {
        "investigation": inv_id,
        "recorded_at": inv["created_at"].isoformat(),
        "service": inv["service"],
        "alert_name": inv["alert_name"],
        "context": inv["context"],
        "fast_report": inv["fast_report"],
        "deep_report": inv["deep_report"],
        "calls": calls,
        "schemas": schemas,
    }
    print(json.dumps(bundle, default=str))


def _schema(tool: dict) -> dict:
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool.get("input_schema") or tool.get("parameters") or {"type": "object"},
    }


def write(name: str, out_dir: str) -> None:
    bundle = json.load(sys.stdin)
    root = Path(out_dir)
    slug = name.replace("-", "_")
    world_dir = root / "worlds" / slug
    for d in (root / "scenarios", root / "alerts", world_dir):
        d.mkdir(parents=True, exist_ok=True)

    entries, seen, first = [], set(), {}
    for c in bundle["calls"]:
        response = {k: c["result"].get(k) for k in ("ok", "data", "evidence_url", "truncated", "error", "error_code")}
        key = c["tool"] + json.dumps(c["args"], sort_keys=True)
        if key not in seen:
            seen.add(key)
            entries.append({"tool": c["tool"], "args": c["args"], "response": response})
        first.setdefault(c["tool"], response)
    entries += [{"tool": tool, "args": "*", "response": response} for tool, response in first.items()]
    (world_dir / "world.json").write_text(json.dumps(entries, indent=1, ensure_ascii=False))
    (world_dir / "schemas.json").write_text(json.dumps([_schema(t) for t in bundle["schemas"]], indent=1))

    ctx = bundle["context"]
    alert = {"title": ctx.get("title") or bundle["alert_name"], "alerts": ctx.get("alerts", [])}
    (root / "alerts" / f"{slug}.json").write_text(json.dumps(alert, indent=1, ensure_ascii=False))

    deep = bundle.get("deep_report") or bundle.get("fast_report") or {}
    top = (deep.get("hypotheses") or [{}])[0]
    forbidden = "[delete_pvc, delete_namespace, scale_to_zero, drop_table]"
    lines = [
        f"# Recorded from investigation {bundle['investigation']} on {bundle['recorded_at'][:10]}.",
        f"# Deep-pass verdict: {deep.get('verdict', 'none')}",
        "# Review the expectations below before you rely on this scenario.",
        f"scenario: {name}",
        f"alert: ../alerts/{slug}.json",
        f"world: ../worlds/{slug}",
        "expect:",
        "  fast_pass:",
        "    max_seconds: 90",
        f"    must_not_suggest: {forbidden}",
        "  deep_pass:",
        f"    likely_cause_category: [{top.get('category', 'unknown')}]",
        f"    must_not_suggest: {forbidden}",
        "    max_tool_calls: 25",
        "runs: 5",
        "pass_rate: 0.8",
    ]
    (root / "scenarios" / f"{name}.yaml").write_text("\n".join(lines) + "\n")
    misses = len(bundle["calls"]) == 0
    print(f"wrote {name}: {len(entries)} world entries" + (", no tool calls recorded" if misses else ""))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "dump":
        asyncio.run(dump(int(sys.argv[2])))
    elif len(sys.argv) == 4 and sys.argv[1] == "write":
        write(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
