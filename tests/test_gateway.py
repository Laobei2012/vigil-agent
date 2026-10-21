import asyncio
import json
import logging
import shutil
import subprocess

import httpx
import pytest
import respx

from vigil_agent.gateway.app import build_registry, create_app
from vigil_agent.gateway.core import Registry, TokenBucket, Tool, ToolOutput, load_allowlist, shape
from vigil_agent.gateway.settings import GatewaySettings
from vigil_agent.gateway.tools.codebase import CodebaseTools
from vigil_agent.gateway.tools.kubernetes import KubernetesTools
from vigil_agent.gateway.tools.logs import LokiLogs, pattern_of
from vigil_agent.gateway.tools.metrics import MetricsTools

PROM = "http://prom:9090"
LOKI = "http://loki:3100"
K8S = "https://k8s"


def settings(**kw) -> GatewaySettings:
    base = dict(
        metrics_url=PROM,
        loki_url=LOKI,
        k8s_api_url=K8S,
        k8s_token_path="/nonexistent",
        k8s_namespace_allowlist="monitoring,oci-cost",
        grafana_url="https://grafana",
    )
    base.update(kw)
    return GatewaySettings(**base)


def echo_registry(**kw) -> tuple[Registry, list]:
    calls = []

    async def fn(x: str) -> ToolOutput:
        calls.append(x)
        return ToolOutput({"x": x})

    reg = Registry(allowlist=frozenset({"query_instant"}), **kw)
    reg.register(
        Tool(
            "query_instant",
            "d",
            {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
            "metrics",
            fn,
        )
    )
    return reg, calls


async def test_unknown_tool_is_rejected_and_audited(caplog):
    reg, calls = echo_registry()
    with caplog.at_level(logging.INFO, logger="vigil.gateway.audit"):
        res = await reg.call("delete_pod", {"x": "1"}, "INC1")
    assert not res.ok and res.error_code == "unknown_tool"
    assert calls == []
    record = json.loads(caplog.records[-1].getMessage())
    assert record["tool"] == "delete_pod" and record["incident_id"] == "INC1"
    assert record["status"] == "unknown_tool"


def test_registering_a_tool_outside_the_allowlist_fails():
    reg = Registry(allowlist=frozenset({"a"}))
    with pytest.raises(ValueError):
        reg.register(Tool("b", "", {"type": "object"}, "x", None))


def test_allowlist_has_only_read_tools():
    names = load_allowlist()
    assert "query_range" in names and "grep_code" in names
    assert not any(w in n for n in names for w in ("delete", "create", "patch", "exec", "scale", "update"))


async def test_invalid_arguments_are_rejected():
    reg, calls = echo_registry()
    assert (await reg.call("query_instant", {}, None)).error_code == "invalid_arguments"
    assert (await reg.call("query_instant", {"x": 1}, None)).error_code == "invalid_arguments"
    assert (await reg.call("query_instant", {"x": "a", "y": 2}, None)).error_code == "invalid_arguments"
    assert calls == []


async def test_audit_record_has_all_fields(caplog):
    reg, _ = echo_registry()
    with caplog.at_level(logging.INFO, logger="vigil.gateway.audit"):
        res = await reg.call("query_instant", {"x": "up"}, "INC2")
    assert res.ok
    record = json.loads(caplog.records[-1].getMessage())
    for key in ("tool", "args", "incident_id", "latency_ms", "bytes", "sha256", "status"):
        assert key in record
    assert len(record["sha256"]) == 64


async def test_rate_limit_applies_per_backend():
    reg, _ = echo_registry(rate_limits={"metrics": TokenBucket(0.0001, 2)})
    results = [await reg.call("query_instant", {"x": "a"}, None) for _ in range(3)]
    assert [r.ok for r in results] == [True, True, False]
    assert results[2].error_code == "rate_limited"


async def test_slow_call_times_out():
    async def slow(x: str) -> ToolOutput:
        await asyncio.sleep(5)
        return ToolOutput({})

    reg = Registry(allowlist=frozenset({"query_instant"}), default_timeout=0.05)
    reg.register(
        Tool("query_instant", "", {"type": "object", "properties": {"x": {"type": "string"}}}, "metrics", slow)
    )
    res = await reg.call("query_instant", {"x": "a"}, None)
    assert res.error_code == "timeout"


async def test_results_are_redacted():
    async def leak(x: str) -> ToolOutput:
        return ToolOutput({"line": "password=hunter2hunter2"})

    reg = Registry(allowlist=frozenset({"query_instant"}))
    reg.register(
        Tool("query_instant", "", {"type": "object", "properties": {"x": {"type": "string"}}}, "metrics", leak)
    )
    res = await reg.call("query_instant", {"x": "a"}, None)
    assert "hunter2hunter2" not in json.dumps(res.data)


def test_shape_truncates_large_results():
    data, truncated = shape({"items": [{"v": "x" * 100} for _ in range(5000)]}, limit=10_000)
    assert truncated and len(json.dumps(data)) <= 10_000


# Metrics


@respx.mock
async def test_query_range_guardrails_and_cap():
    m = MetricsTools(settings(), httpx.AsyncClient())
    with pytest.raises(Exception, match="7 days"):
        await m.query_range("up", "now-8d")
    with pytest.raises(Exception, match="15s"):
        await m.query_range("up", "now-1h", step="5s")
    series = [{"metric": {"i": str(i)}, "values": [[1, "1"], [2, "3"]]} for i in range(80)]
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"result": series}})
    )
    out = await m.query_range("rate(x[5m])", "now-1h")
    assert out.truncated and len(out.data["series"]) == 50
    assert out.data["series"][0]["max"] == 3.0
    assert out.evidence_url.startswith("https://grafana/explore?")
    assert "rate%28x%5B5m%5D%29" in out.evidence_url


@respx.mock
async def test_query_timeout_is_passed_to_backend():
    route = respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"result": []}})
    )
    await MetricsTools(settings(), httpx.AsyncClient()).query_instant("up")
    assert route.calls[0].request.url.params["timeout"] == "20s"


# Kubernetes


async def test_k8s_refuses_secrets_namespaces_and_long_tails():
    k = KubernetesTools(settings(), httpx.AsyncClient())
    with pytest.raises(Exception, match="not readable"):
        await k.k8s_get("secrets", "x", "monitoring")
    with pytest.raises(Exception, match="allowlist"):
        await k.k8s_list("pods", "kube-system")
    with pytest.raises(Exception, match="at most 500"):
        await k.k8s_pod_logs("monitoring", "p", tail=501)


@respx.mock
async def test_k8s_configmap_values_are_masked():
    respx.get(f"{K8S}/api/v1/namespaces/monitoring/configmaps/c").mock(
        return_value=httpx.Response(
            200,
            json={
                "kind": "ConfigMap",
                "metadata": {"name": "c", "managedFields": [{"x": 1}]},
                "data": {"DB_PASSWORD": "abc", "LOG_LEVEL": "info", "url": "postgres://u:pw1234@h/db"},
            },
        )
    )
    k = KubernetesTools(settings(), httpx.AsyncClient())
    reg = Registry(allowlist=load_allowlist())
    for t in k.tools():
        reg.register(t)
    res = await reg.call("k8s_get", {"resource": "configmaps", "name": "c", "namespace": "monitoring"}, None)
    assert res.ok
    assert res.data["data"]["DB_PASSWORD"] == "[REDACTED]"
    assert res.data["data"]["LOG_LEVEL"] == "info"
    assert "pw1234" not in json.dumps(res.data)
    assert "managedFields" not in res.data["metadata"]


@respx.mock
async def test_k8s_list_summarizes_pods():
    respx.get(f"{K8S}/api/v1/namespaces/oci-cost/pods").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {
                            "name": "p1",
                            "namespace": "oci-cost",
                            "creationTimestamp": "2026-10-01T00:00:00Z",
                        },
                        "spec": {"nodeName": "n1"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "ready": False,
                                    "restartCount": 4,
                                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                                    "lastState": {"terminated": {"reason": "OOMKilled"}},
                                }
                            ],
                        },
                    }
                ]
            },
        )
    )
    out = await KubernetesTools(settings(), httpx.AsyncClient()).k8s_list("pods", "oci-cost")
    row = out.data["items"][0]
    assert row["restarts"] == 4 and row["ready"] == "0/1"
    assert row["reasons"] == ["CrashLoopBackOff", "OOMKilled"]


# Logs


def test_log_patterns_group_by_shape():
    assert pattern_of("timeout after 30s to 10.0.0.1") == pattern_of("timeout after 12s to 10.0.0.9")


@respx.mock
async def test_loki_search_summarizes():
    values = [[str(1_760_000_000_000_000_000 + i), f"error: connect to db failed attempt {i}"] for i in range(30)]
    respx.get(f"{LOKI}/loki/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "result": [{"stream": {"namespace": "oci-cost", "pod": "p", "secret_label": "x"}, "values": values}]
                }
            },
        )
    )
    out = await LokiLogs(settings(), httpx.AsyncClient()).search_logs('{namespace="oci-cost"} |= "error"')
    assert out.data["hits"] == 30
    assert out.data["top_patterns"][0]["count"] == 30
    assert len(out.data["sample"]) == 20
    assert "secret_label" not in out.data["sample"][0]["labels"]
    assert "loki" in out.evidence_url


async def test_loki_rejects_query_without_selector_and_long_range():
    logs = LokiLogs(settings(), httpx.AsyncClient())
    with pytest.raises(Exception, match="selector"):
        await logs.search_logs("error")
    with pytest.raises(Exception, match="14 days"):
        await logs.search_logs('{namespace="x"}', start="now-15d")


# Codebase


@pytest.fixture
def git_repo(tmp_path):
    if not shutil.which("rg"):
        pytest.skip("ripgrep is not installed")
    repo = tmp_path / "src"
    repo.mkdir()

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "app.py").write_text("def handler():\n    raise TimeoutError('upstream timeout')\n")
    git("add", ".")
    git("commit", "-qm", "one")
    first = git("rev-parse", "HEAD")
    (repo / "app.py").write_text("def handler():\n    return 'ok'\n")
    git("commit", "-qam", "two")
    return repo, first


async def test_code_reads_stay_on_the_given_sha(git_repo, tmp_path):
    repo, first = git_repo
    s = settings(
        codebase_backend="git",
        github_repo_allowlist="org/app",
        codebase_cache_dir=str(tmp_path / "cache"),
        git_url_template=f"file://{repo}",
    )
    code = CodebaseTools(s, httpx.AsyncClient(), None)
    found = await code.grep_code("upstream timeout", first)
    assert found.data["matches"][0]["path"] == "app.py"
    read = await code.read_file("app.py", first)
    assert "TimeoutError" in read.data["content"]
    with pytest.raises(Exception, match="without"):
        await code.read_file("../etc/passwd", first)
    with pytest.raises(Exception, match="without"):
        await code.read_file("/etc/passwd", first)
    with pytest.raises(Exception, match="allowlist"):
        await code.grep_code("x", first, repo="other/repo")


async def test_read_file_caps_lines(git_repo, tmp_path):
    repo, _ = git_repo
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(1000)))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "big"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    s = settings(
        codebase_backend="git",
        github_repo_allowlist="org/app",
        codebase_cache_dir=str(tmp_path / "cache"),
        git_url_template=f"file://{repo}",
    )
    out = await CodebaseTools(s, httpx.AsyncClient(), None).read_file("big.txt", sha)
    assert out.data["end_line"] - out.data["start_line"] + 1 == 400
    assert out.truncated


# App


def test_registry_for_test_cluster_has_expected_tools():
    reg = build_registry(settings())
    names = set(reg.tools)
    assert {
        "query_range",
        "search_logs",
        "k8s_events",
        "grep_code",
        "get_change_timeline",
        "resolve_deployed_sha",
    } <= names
    assert not {"search_issues", "get_page", "list_changes"} & names


async def test_app_requires_token():
    s = settings(token="t0k")
    app = create_app(s, build_registry(s))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
        assert (await c.get("/v1/tools")).status_code == 401
        ok = await c.get("/v1/tools", headers={"Authorization": "Bearer t0k"})
        assert ok.status_code == 200 and any(t["name"] == "query_range" for t in ok.json())
        res = await c.post("/v1/tools/rm_rf", json={"args": {}}, headers={"Authorization": "Bearer t0k"})
        assert res.json()["error_code"] == "unknown_tool"
