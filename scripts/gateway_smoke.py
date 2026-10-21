"""Call each gateway tool once against live backends and print a one-line result per call.

Usage: VIGIL_GATEWAY_TOKEN=... uv run python scripts/gateway_smoke.py http://localhost:18081
Exits 1 if a tool that should work fails, or a guardrail that should refuse does not.
"""

import asyncio
import json
import os
import sys

import httpx

OK = [
    ("query_instant", {"query": "up"}),
    ("query_range", {"query": "sum(rate(container_cpu_usage_seconds_total[5m]))", "start": "now-1h"}),
    ("list_alert_rules", {"name_filter": "LonghornVolume"}),
    ("series_labels", {"match": 'up{namespace="monitoring"}'}),
    ("search_logs", {"query": '{namespace="flux-system"} |~ "(?i)error"', "start": "now-6h", "size": 50}),
    ("logs_count", {"query": '{namespace="loki"}', "start": "now-1h"}),
    ("aggregate_terms", {"query": '{namespace="monitoring"}', "field": "pod", "start": "now-1h"}),
    ("k8s_list", {"resource": "pods", "namespace": "oci-cost"}),
    ("k8s_list", {"resource": "nodes"}),
    ("k8s_list", {"resource": "helmreleases", "namespace": "monitoring"}),
    ("k8s_list", {"resource": "volumes.longhorn.io", "namespace": "longhorn"}),
    ("k8s_list", {"resource": "certificatesigningrequests"}),
    ("k8s_get", {"resource": "kustomizations", "name": "flux-system", "namespace": "flux-system"}),
    ("k8s_describe", {"resource": "clusters.postgresql.cnpg.io", "name": "oci-cost", "namespace": "oci-cost"}),
    ("k8s_events", {"namespace": "vigil-agent", "since": "now-2h"}),
    ("k8s_pod_logs", {"namespace": "loki", "pod": "loki-0", "tail": 20}),
    ("k8s_top", {}),
    ("resolve_deployed_sha", {"service": "oci-cost"}),
    ("get_change_timeline", {"service": "oci-cost", "since": "now-24h"}),
]
REFUSE = [
    ("k8s_get", {"resource": "secrets", "name": "x", "namespace": "oci-cost"}, "forbidden"),
    ("k8s_pod_logs", {"namespace": "loki", "pod": "loki-0", "tail": 900}, "invalid_arguments"),
    ("query_range", {"query": "up", "start": "now-30d"}, "invalid_arguments"),
    ("query_range", {"query": "up", "start": "now-1h", "step": "1s"}, "invalid_arguments"),
    ("read_file", {"path": "../../etc/passwd", "sha": "0" * 40}, "invalid_arguments"),
    ("delete_namespace", {"name": "oci-cost"}, "unknown_tool"),
]


async def main(base: str) -> int:
    headers = {"Authorization": f"Bearer {os.environ['VIGIL_GATEWAY_TOKEN']}"}
    failures = 0
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=90) as c:
        tools = {t["name"] for t in (await c.get("/v1/tools")).json()}
        print(f"{len(tools)} tools: {', '.join(sorted(tools))}")

        async def call(name, args):
            return (await c.post(f"/v1/tools/{name}", json={"args": args, "incident_id": "smoke"})).json()

        sha = None
        for name, args in OK:
            r = await call(name, args)
            size = len(json.dumps(r.get("data")))
            if name == "resolve_deployed_sha" and r["ok"]:
                sha = r["data"]["sha"]
            mark = "ok  " if r["ok"] else "FAIL"
            failures += not r["ok"]
            detail = r.get("error") or _summary(name, r["data"])
            print(f"{mark} {name} {json.dumps(args)[:70]} -> {size}B trunc={r['truncated']} {detail}")
        if sha:
            for name, args in [
                ("grep_code", {"pattern": "LonghornVolumeFaulted", "sha": sha}),
                ("read_file", {"path": "README.md", "sha": sha, "end_line": 5}),
            ]:
                r = await call(name, args)
                failures += not r["ok"]
                print(f"{'ok  ' if r['ok'] else 'FAIL'} {name} -> {r.get('error') or _summary(name, r['data'])}")
            r = await call("grep_code", {"pattern": "x", "sha": "1" * 40})
            ok = r.get("error_code") == "revision_unavailable"
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} grep_code at another revision refused: {r.get('error')}")
        for name, args, code in REFUSE:
            r = await call(name, args)
            ok = not r["ok"] and r.get("error_code") == code
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} refused {name} {json.dumps(args)[:60]} -> {r.get('error_code')}")
    print("all gateway checks passed" if not failures else f"{failures} gateway check(s) failed")
    return 1 if failures else 0


def _summary(name: str, data) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("series_count", "count", "hits"):
        if key in data:
            return f"{key}={data[key]}"
    if "matches" in data:
        return f"matches={len(data['matches'])} first={data['matches'][:1]}"
    if "sha" in data:
        return f"sha={data['sha'][:12]} source={data.get('source')}"
    if "items" in data:
        return f"items={len(data['items'])}"
    if "content" in data:
        return data["content"].splitlines()[0][:60]
    return ",".join(list(data)[:5])


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:18081")))
