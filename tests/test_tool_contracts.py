"""Contract tests for the production backends, against recorded response shapes."""

import httpx
import pytest
import respx

from vigil_agent.gateway.settings import GatewaySettings
from vigil_agent.gateway.tools.atlassian import ConfluenceTools, JiraTools
from vigil_agent.gateway.tools.github import GitHubTools
from vigil_agent.gateway.tools.kubernetes import KubernetesTools
from vigil_agent.gateway.tools.logs import ElasticsearchLogs

ES = "http://es:9200"
JIRA = "https://jira.example"
WIKI = "https://wiki.example"
GH = "https://api.github.com"


def s(**kw) -> GatewaySettings:
    base = dict(
        es_url=ES,
        es_index_allowlist="logs-checkout-*,logs-pay-*",
        kibana_url="https://kibana.example",
        jira_url=JIRA,
        jira_project_allowlist="OPS,INC",
        confluence_url=WIKI,
        confluence_space_allowlist="RUN",
        github_token="t",
        github_repo_allowlist="org/checkout",
        k8s_api_url="https://k8s",
        k8s_token_path="/nonexistent",
    )
    base.update(kw)
    return GatewaySettings(**base)


@respx.mock
async def test_elasticsearch_search_filters_fields_and_index():
    route = respx.post(f"{ES}/logs-checkout-2026.10.21/_search").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "@timestamp": "2026-10-21T07:00:00Z",
                                "message": "timeout to db 10.0.0.1",
                                "user": {"email": "a@b.c"},
                                "log": {"level": "error"},
                            }
                        }
                    ]
                    * 3
                }
            },
        )
    )
    es = ElasticsearchLogs(s(), httpx.AsyncClient())
    out = await es.search_logs("level:error", index_pattern="logs-checkout-2026.10.21", size=50)
    body = route.calls[0].request.content.decode()
    assert '"size":50' in body.replace(" ", "")
    assert out.data["hits"] == 3 and out.data["top_patterns"][0]["count"] == 3
    assert "user.email" not in out.data["sample"][0]["labels"]
    assert out.evidence_url.startswith("https://kibana.example/app/discover")
    with pytest.raises(Exception, match="allowlist"):
        await es.search_logs("x", index_pattern="secrets-*")
    with pytest.raises(Exception, match="required"):
        await es.search_logs("x")


@respx.mock
async def test_elasticsearch_count_and_terms():
    respx.post(f"{ES}/logs-pay-1/_count").mock(return_value=httpx.Response(200, json={"count": 42}))
    respx.post(f"{ES}/logs-pay-1/_search").mock(
        return_value=httpx.Response(
            200, json={"aggregations": {"terms": {"buckets": [{"key": "checkout", "doc_count": 40}]}}}
        )
    )
    es = ElasticsearchLogs(s(), httpx.AsyncClient())
    assert (await es.logs_count("*", index_pattern="logs-pay-1")).data["count"] == 42
    terms = await es.aggregate_terms("*", "service.name", index_pattern="logs-pay-1")
    assert terms.data["buckets"] == [{"key": "checkout", "count": 40}]
    with pytest.raises(Exception, match="field"):
        await es.aggregate_terms("*", "user.email", index_pattern="logs-pay-1")


@respx.mock
async def test_jira_limits_projects_and_jql_length():
    route = respx.get(f"{JIRA}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(
            200, json={"issues": [{"key": "INC-7", "fields": {"summary": "checkout 5xx", "status": {"name": "Done"}}}]}
        )
    )
    jira = JiraTools(s(), httpx.AsyncClient())
    out = await jira.search_issues('text ~ "checkout"')
    assert 'project in ("OPS","INC")' in route.calls[0].request.url.params["jql"]
    assert out.data["issues"][0]["status"] == "Done"
    with pytest.raises(Exception, match="longer"):
        await jira.search_issues("x" * 501)
    with pytest.raises(Exception, match="allowlist"):
        await jira.get_issue("HR-1")


@respx.mock
async def test_confluence_space_allowlist_and_truncation():
    respx.get(f"{WIKI}/rest/api/content/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "title": "Runbook",
                "space": {"key": "RUN"},
                "body": {"storage": {"value": "<p>" + "x" * 30000 + "</p>"}},
                "_links": {"webui": "/spaces/RUN/pages/1"},
            },
        )
    )
    respx.get(f"{WIKI}/rest/api/content/2").mock(
        return_value=httpx.Response(
            200, json={"title": "Salaries", "space": {"key": "HR"}, "body": {"storage": {"value": "secret"}}}
        )
    )
    wiki = ConfluenceTools(s(), httpx.AsyncClient())
    page = await wiki.get_page("1")
    assert len(page.data["body"]) == 20000 and page.truncated
    with pytest.raises(Exception, match="allowlist"):
        await wiki.get_page("2")


@respx.mock
async def test_github_changes_and_diff_caps():
    repo = "org/checkout"
    commits = [
        {
            "sha": f"{i:040x}",
            "html_url": "u",
            "author": {"login": "dev"},
            "commit": {
                "message": f"change {i}\n\nbody",
                "committer": {"date": "2026-10-21T06:00:00Z"},
                "author": {"name": "dev"},
            },
        }
        for i in range(60)
    ]
    respx.get(f"{GH}/repos/{repo}/commits").mock(return_value=httpx.Response(200, json=commits))
    respx.get(f"{GH}/repos/{repo}/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 5,
                    "title": "bump chart",
                    "merged_at": "2026-10-21T06:30:00Z",
                    "user": {"login": "renovate"},
                    "merge_commit_sha": "abc",
                    "html_url": "u",
                }
            ],
        )
    )
    respx.get(f"{GH}/repos/{repo}/deployments").mock(return_value=httpx.Response(200, json=[]))
    diff = "".join(f"diff --git a/f{i}.py b/f{i}.py\n" + "+x\n" * 100 for i in range(30))
    respx.get(f"{GH}/repos/{repo}/compare/a...b").mock(return_value=httpx.Response(200, text=diff))
    gh = GitHubTools(s(), httpx.AsyncClient(), KubernetesTools(s(), httpx.AsyncClient()), None)
    out = await gh.list_changes(since="2026-10-21T00:00:00Z", until="2026-10-21T08:00:00Z")
    assert out.truncated and len(out.data["changes"]) == 50
    assert any(c["source"] == "github_pr" for c in out.data["changes"])
    d = await gh.get_diff("a", "b")
    assert d.truncated and d.data["diff"].count("\n") < 2000
    only = await gh.get_diff("a", "b", path_filter="f1.py")
    assert only.data["diff"].startswith("diff --git a/f1.py")
    with pytest.raises(Exception, match="allowlist"):
        await gh.list_changes(repo="org/other")


@respx.mock
async def test_deployed_sha_from_image_label():
    respx.get("https://k8s/api/v1/namespaces/checkout/pods").mock(
        return_value=httpx.Response(
            200, json={"items": [{"status": {"containerStatuses": [{"imageID": "ghcr.io/org/checkout@sha256:aaa"}]}}]}
        )
    )
    respx.get("https://ghcr.io/v2/org/checkout/manifests/sha256:aaa").mock(
        return_value=httpx.Response(200, json={"config": {"digest": "sha256:cfg"}})
    )
    respx.get("https://ghcr.io/v2/org/checkout/blobs/sha256:cfg").mock(
        return_value=httpx.Response(200, json={"config": {"Labels": {"org.opencontainers.image.revision": "4f2a9c1"}}})
    )
    gh = GitHubTools(s(codebase_backend="git"), httpx.AsyncClient(), KubernetesTools(s(), httpx.AsyncClient()), None)
    out = await gh.resolve_deployed_sha(namespace="checkout")
    assert out.data["sha"] == "4f2a9c1"
    cached = await gh.resolve_deployed_sha(namespace="checkout")
    assert cached.data == out.data and respx.calls.call_count == 3


@respx.mock
async def test_deployed_sha_without_label_is_a_clear_error():
    respx.get("https://k8s/api/v1/namespaces/checkout/pods").mock(
        return_value=httpx.Response(
            200, json={"items": [{"status": {"containerStatuses": [{"imageID": "ghcr.io/org/checkout@sha256:aaa"}]}}]}
        )
    )
    respx.get("https://ghcr.io/v2/org/checkout/manifests/sha256:aaa").mock(
        return_value=httpx.Response(200, json={"config": {"digest": "sha256:cfg"}})
    )
    respx.get("https://ghcr.io/v2/org/checkout/blobs/sha256:cfg").mock(
        return_value=httpx.Response(200, json={"config": {"Labels": {}}})
    )
    gh = GitHubTools(s(codebase_backend="git"), httpx.AsyncClient(), KubernetesTools(s(), httpx.AsyncClient()), None)
    with pytest.raises(Exception, match="org.opencontainers.image.revision"):
        await gh.resolve_deployed_sha(namespace="checkout")


JENKINS = "http://jenkins:8080"


def jenkins_settings(**kw):
    return s(
        jenkins_url=JENKINS,
        jenkins_public_url="https://jenkins.example",
        jenkins_user="vigil",
        jenkins_token="t",
        jenkins_job_allowlist="tts,folder/*",
        **kw,
    )


@respx.mock
async def test_jenkins_build_log_returns_tail_and_error_lines():
    from vigil_agent.gateway.tools.jenkins import JenkinsTools

    respx.get(f"{JENKINS}/job/tts/lastFailedBuild/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 42,
                "result": "FAILURE",
                "timestamp": 1792600000000,
                "duration": 65000,
                "builtOn": "win10-gpu",
                "actions": [{"causes": [{"shortDescription": "Started by user simon"}]}, {}],
            },
        )
    )
    log = "\n".join(
        [
            "Running on win10-gpu",
            "C:\\jenkins>tts.bat 20",
            "ERROR: NAS share \\\\nas\\audio not found",
            "token=abcdefabcdefabcdef",
            "Build step 'Execute Windows batch command' marked build as failure",
            "Finished: FAILURE",
        ]
    )
    route = respx.get(f"{JENKINS}/job/tts/lastFailedBuild/consoleText").mock(return_value=httpx.Response(200, text=log))
    j = JenkinsTools(jenkins_settings(), httpx.AsyncClient())
    out = await j.jenkins_build_log("tts", tail_lines=3)
    assert route.calls[0].request.headers["authorization"].startswith("Basic ")
    assert (
        out.data["build"] == 42 and out.data["agent"] == "win10-gpu" and out.data["causes"] == ["Started by user simon"]
    )
    assert [e["line"] for e in out.data["error_lines"]] == [3, 5, 6]
    assert out.data["tail"][-1] == "Finished: FAILURE" and len(out.data["tail"]) == 3 and out.truncated
    assert out.evidence_url == "https://jenkins.example/job/tts/42/console"


async def test_jenkins_refuses_jobs_outside_the_allowlist_and_bad_builds():
    from vigil_agent.gateway.tools.jenkins import JenkinsTools

    j = JenkinsTools(jenkins_settings(), httpx.AsyncClient())
    with pytest.raises(Exception, match="allowlist"):
        await j.jenkins_build_log("deploy-prod")
    with pytest.raises(Exception, match="job name"):
        await j.jenkins_build_log("../../script")
    with pytest.raises(Exception, match="build must be"):
        await j.jenkins_build_log("tts", build="1/../../script")
    with pytest.raises(Exception, match="at most"):
        await j.jenkins_build_log("tts", tail_lines=900)


@respx.mock
async def test_jenkins_job_status_in_a_folder():
    from vigil_agent.gateway.tools.jenkins import JenkinsTools

    respx.get(f"{JENKINS}/job/folder/job/app/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "builds": [
                    {"number": 7, "result": None, "building": True, "timestamp": 1792600000000, "duration": 0},
                    {"number": 6, "result": "FAILURE", "timestamp": 1792500000000, "duration": 1000, "builtOn": "mac"},
                ],
                "lastSuccessfulBuild": {"number": 5},
                "lastFailedBuild": {"number": 6},
                "inQueue": False,
            },
        )
    )
    out = await JenkinsTools(jenkins_settings(), httpx.AsyncClient()).jenkins_job_status("folder/app")
    assert out.data["builds"][0]["result"] == "BUILDING" and out.data["builds"][1]["agent"] == "mac"
    assert out.data["last_failed"] == 6 and out.evidence_url == "https://jenkins.example/job/folder/job/app/"


def test_jenkins_tools_register_only_with_a_token():
    from vigil_agent.gateway.app import build_registry

    assert "jenkins_build_log" not in build_registry(s(jenkins_url=JENKINS)).tools
    assert {"jenkins_build_log", "jenkins_job_status"} <= set(build_registry(jenkins_settings()).tools)
