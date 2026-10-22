import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
import respx

from tests.fakes import FakeModel, msg, report_args, tool_use
from vigil_agent.catalog import Catalog, Flags, ReloadingFile
from vigil_agent.config import Settings
from vigil_agent.intake.events import (
    MAX_ALERTS,
    MAX_VALUE_CHARS,
    Alert,
    IntakeEvent,
    parse_alertmanager,
    parse_pd_webhook,
    pd_alert_to_alert,
    verify_pd_signature,
)
from vigil_agent.intake.receiver import MAX_BODY_BYTES, InboxQueue, create_receiver
from vigil_agent.orchestrator.intake import Intake, route
from vigil_agent.orchestrator.investigation import Investigator
from vigil_agent.orchestrator.prefetch import plan, prefetch
from vigil_agent.orchestrator.publisher import Publisher
from vigil_agent.orchestrator.runtime import Runtime
from vigil_agent.pagerduty import PagerDutyClient

FIX = Path(__file__).parent / "fixtures"
SECRET = "whsec_test"

CATALOG = {
    "defaults": {"fallback_channel": "#vigil-fallback", "unmapped_enabled": False},
    "services": [
        {
            "name": "oci-cost",
            "vigil_enabled": True,
            "pd_service_ids": ["PSVC123"],
            "team": "sre",
            "slack_channel": "#vigil-test",
            "namespaces": ["oci-cost"],
            "repos": ["Laobei2012/oci-free-cloud-k8s"],
            "runbook": "README.md",
            "context_packs": [
                {
                    "alert_name": "KubeJobFailed",
                    "metric_queries": ['kube_job_failed{namespace="oci-cost"}'],
                    "log_query": '{namespace="oci-cost"} |= "error"',
                }
            ],
        },
        {
            "name": "jenkins",
            "vigil_enabled": False,
            "team": "sre",
            "slack_channel": "#vigil-test",
            "namespaces": ["jenkins"],
        },
    ],
}


class RecordingSlack:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []

    async def post(self, channel, text, blocks=None, thread_ts=None):
        ts = f"{len(self.posts) + 1}.0"
        self.posts.append({"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts, "ts": ts})
        return ts

    async def update(self, channel, ts, text, blocks=None):
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})

    def thread(self, ts):
        return [p for p in self.posts if p["thread_ts"] == ts]


class FakeGateway:
    def __init__(self, fail=(), slow=()):
        self.calls = []
        self.fail = set(fail)
        self.slow = set(slow)

    async def schemas(self):
        return [
            {"name": n, "description": "", "input_schema": {"type": "object"}}
            for n in ("query_range", "search_logs", "k8s_events", "get_change_timeline", "list_alert_rules")
        ]

    async def call(self, name, args, incident_id):
        self.calls.append((name, args))
        if name in self.slow:
            await asyncio.sleep(5)
        if name in self.fail:
            return {"ok": False, "error": "backend down", "error_code": "backend_error"}
        data = {
            "list_alert_rules": {
                "rules": [
                    {
                        "name": "KubeJobFailed",
                        "query": "kube_job_failed > 0",
                        "annotations": {"description": "Job failed."},
                    }
                ]
            },
            "get_change_timeline": {
                "entries": [{"time": "2026-10-20T06:00:00Z", "source": "github_pr", "summary": "bump collector"}],
                "gaps": [],
            },
            "k8s_events": {
                "events": [
                    {
                        "time": "t",
                        "type": "Warning",
                        "reason": "BackoffLimitExceeded",
                        "object": "Job/x",
                        "message": "Job has reached the backoff limit",
                    }
                ]
            },
        }.get(name, {"series": [], "hits": 0, "top_patterns": [], "sample": []})
        return {"ok": True, "data": data, "evidence_url": f"https://monitoring.oci.liwenxin.com/{name}"}


def make(store, *, model=None, gateway=None, pd_key=None, **kw):
    s = Settings(
        database_url="unused",
        model_enabled=model is not None,
        pd_webhook_secrets=SECRET,
        pd_api_key=pd_key,
        owner="pod-a",
        fast_deadline_seconds=60,
        prefetch_timeout_seconds=2,
        deep_max_seconds=30,
        **kw,
    )
    catalog = ReloadingFile(None, None, Catalog.from_dict(CATALOG))
    flags = ReloadingFile(None, None, kw.pop("flags", Flags()) if "flags" in kw else Flags())
    slack = RecordingSlack()
    pub = Publisher(s, store, slack)
    pd = PagerDutyClient(pd_key, "https://pd.test", retries=2, backoff=0)
    intake = Intake(s, store, catalog, flags, pd, pub)
    gw = gateway or FakeGateway()
    inv = Investigator(s, store, catalog, gw, model, pub, pd, s.owner)
    return s, intake, inv, slack, Runtime(s, store, intake, inv, pub), flags


def pd_event(kind="incident.triggered", event_id="E1", incident="Q2K8TESTINC01") -> IntakeEvent:
    body = json.loads((FIX / "pd" / "incident_triggered.json").read_text())
    body["event"]["event_type"] = kind
    body["event"]["id"] = event_id
    body["event"]["data"]["id"] = incident
    return parse_pd_webhook(body)


def am_payload(status="firing", namespace="oci-cost", alertname="KubeJobFailed", cluster=None, fp="f1"):
    labels = {"alertname": alertname, "namespace": namespace, "severity": "warning"}
    if cluster:
        labels["cluster"] = cluster
    return {
        "version": "4",
        "groupKey": f'{{}}:{{alertname="{alertname}", namespace="{namespace}"}}',
        "status": status,
        "receiver": "vigil",
        "groupLabels": {"alertname": alertname, "namespace": namespace},
        "commonLabels": labels,
        "commonAnnotations": {"summary": "Job failed."},
        "externalURL": "http://am",
        "alerts": [
            {
                "status": status,
                "labels": labels,
                "annotations": {"summary": "Job failed."},
                "startsAt": "2026-10-20T06:00:00Z",
                "fingerprint": fp,
            }
        ],
    }


# Parsing and signatures


def test_signature_accepts_any_matching_v1():
    body = b'{"a":1}'
    good = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_pd_signature(body, f"v1=deadbeef,v1={good}", [SECRET])
    assert not verify_pd_signature(body + b" ", f"v1={good}", [SECRET])
    assert not verify_pd_signature(body, f"v1={good}", ["other"])
    assert not verify_pd_signature(body, None, [SECRET])


def test_pd_alert_labels_from_default_alertmanager_details():
    raw = json.loads((FIX / "pd" / "incident_alerts.json").read_text())["alerts"][0]
    alert = pd_alert_to_alert(raw)
    assert alert.labels["alertname"] == "KubeJobFailed" and alert.labels["namespace"] == "oci-cost"
    assert "failed to complete" in alert.annotations["description"]


def test_pd_alert_labels_from_structured_firing_details():
    # PagerDuty parses Alertmanager's "firing" text into lists. A value with ": " becomes a one-key dict.
    firing = {
        "Annotations": [
            {"description = Console log": "https://ci.example.com/job/build-app/lastFailedBuild/console"},
            "summary = The last build of the Jenkins job build-app failed.",
        ],
        "Labels": ["alertname = JenkinsJobFailed", "jenkins_job = build-app", "namespace = jenkins", "pod = ci-0"],
        "Source": "http://prometheus:9090/graph",
    }
    details = {"cluster": "oci-phx", "namespace": "jenkins", "alertname": "JenkinsJobFailed", "firing": firing}
    alert = pd_alert_to_alert({"body": {"details": details}})
    assert alert.labels["jenkins_job"] == "build-app" and alert.labels["pod"] == "ci-0"
    assert alert.labels["cluster"] == "oci-phx"
    assert (
        alert.annotations["description"] == "Console log: https://ci.example.com/job/build-app/lastFailedBuild/console"
    )
    assert alert.annotations["summary"] == "The last build of the Jenkins job build-app failed."
    # Several alerts in one group arrive as a list. The first alert's value wins.
    two = pd_alert_to_alert({"body": {"details": {"firing": [firing, {"Labels": ["pod = ci-1"]}]}}})
    assert two.labels["pod"] == "ci-0"


def test_pd_alert_labels_from_explicit_details():
    alert = pd_alert_to_alert({"body": {"details": {"cluster": "oci-phx", "namespace": "x", "alertname": "A"}}})
    assert alert.labels == {"cluster": "oci-phx", "namespace": "x", "alertname": "A"}


def test_alertmanager_event_ids_dedupe_repeats():
    a, b = parse_alertmanager(am_payload()), parse_alertmanager(am_payload())
    assert a.event_id == b.event_id and a.incident_id == b.incident_id
    r = parse_alertmanager(am_payload(status="resolved"))
    assert r.event_type == "incident.resolved" and r.incident_id == a.incident_id and r.event_id != a.event_id


def test_routing():
    ev = IntakeEvent(
        source="alertmanager",
        event_id="e",
        event_type="incident.triggered",
        incident_id="i",
        title="t",
        alerts=[Alert(labels={"cluster": "oci-phx"}), Alert(labels={"cluster": "eu"})],
    )
    assert route(ev, "oci-phx", False) == (True, "cluster_label") and len(ev.alerts) == 1
    other = IntakeEvent(
        source="alertmanager",
        event_id="e",
        event_type="incident.triggered",
        incident_id="i",
        title="t",
        alerts=[Alert(labels={"cluster": "eu"})],
    )
    assert route(other, "oci-phx", True) == (False, "other_cluster")
    none = IntakeEvent(
        source="alertmanager",
        event_id="e",
        event_type="incident.triggered",
        incident_id="i",
        title="t",
        alerts=[Alert(labels={})],
    )
    assert route(none, "oci-phx", True)[0] and not route(none, "oci-phx", False)[0]


# Receiver


async def test_receiver(store):
    s = Settings(database_url="unused", pd_webhook_secrets=SECRET, am_webhook_token="tok")
    app = create_receiver(s, InboxQueue(store))
    body = (FIX / "pd" / "incident_triggered.json").read_bytes()
    sig = "v1=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://r") as c:
        assert (await c.post("/webhooks/pagerduty", content=body)).status_code == 401
        assert (
            await c.post("/webhooks/pagerduty", content=body + b" ", headers={"X-PagerDuty-Signature": sig})
        ).status_code == 401
        ok = await c.post("/webhooks/pagerduty", content=body, headers={"X-PagerDuty-Signature": sig})
        assert ok.status_code == 202
        other = json.loads(body)
        other["event"]["event_type"] = "incident.priority_updated"
        raw = json.dumps(other).encode()
        sig2 = "v1=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        ignored = await c.post("/webhooks/pagerduty", content=raw, headers={"X-PagerDuty-Signature": sig2})
        assert ignored.json()["status"] == "ignored"
        am = await c.post("/webhooks/alertmanager", json=am_payload(), headers={"Authorization": "Bearer tok"})
        assert am.status_code == 202
    assert len(await store.fetchall("SELECT * FROM inbox")) == 2


async def test_receiver_returns_500_when_enqueue_fails():
    class Broken:
        async def put(self, event):
            raise RuntimeError("db down")

    app = create_receiver(Settings(database_url="unused", am_webhook_token="tok"), Broken())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://r") as c:
        res = await c.post("/webhooks/alertmanager", json=am_payload(), headers={"Authorization": "Bearer tok"})
        assert res.status_code == 500


async def test_alertmanager_token(store):
    app = create_receiver(Settings(database_url="unused", am_webhook_token="tok"), InboxQueue(store))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://r") as c:
        assert (await c.post("/webhooks/alertmanager", json=am_payload())).status_code == 401
        odd = {"Authorization": "Bearer t\xf6k".encode("latin-1")}
        assert (await c.post("/webhooks/alertmanager", json=am_payload(), headers=odd)).status_code == 401
        ok = await c.post("/webhooks/alertmanager", json=am_payload(), headers={"Authorization": "Bearer tok"})
        assert ok.status_code == 202


async def test_alertmanager_route_is_off_without_a_token(store):
    app = create_receiver(Settings(database_url="unused"), InboxQueue(store))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://r") as c:
        assert (await c.post("/webhooks/alertmanager", json=am_payload())).status_code == 503
    assert await store.fetchall("SELECT * FROM inbox") == []


async def test_receiver_rejects_large_bodies(store):
    s = Settings(database_url="unused", pd_webhook_secrets=SECRET, am_webhook_token="tok")
    app = create_receiver(s, InboxQueue(store))
    big = b"x" * (MAX_BODY_BYTES + 1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://r") as c:
        assert (await c.post("/webhooks/pagerduty", content=big)).status_code == 413
        res = await c.post("/webhooks/alertmanager", content=big, headers={"Authorization": "Bearer tok"})
        assert res.status_code == 413


def test_alert_fields_are_redacted_at_intake():
    payload = am_payload()
    payload["alerts"][0]["annotations"] = {"description": "login failed with password=hunter2fake"}
    payload["commonAnnotations"] = {"summary": "token=abcdefabcdefabcdef leaked"}
    event = parse_alertmanager(payload)
    assert "hunter2fake" not in event.alerts[0].annotations["description"]
    assert "abcdefabcdefabcdef" not in event.title


def test_alertmanager_events_cap_the_alerts_and_the_values():
    payload = am_payload()
    payload["alerts"] = [dict(payload["alerts"][0], fingerprint=str(i)) for i in range(MAX_ALERTS + 50)]
    payload["alerts"][0] = dict(payload["alerts"][0], annotations={"description": "d" * (MAX_VALUE_CHARS + 1)})
    payload["commonAnnotations"] = {"summary": "s" * (MAX_VALUE_CHARS + 1)}
    event = parse_alertmanager(payload)
    assert len(event.alerts) == MAX_ALERTS
    assert len(event.alerts[0].annotations["description"]) == MAX_VALUE_CHARS
    assert len(event.title) == MAX_VALUE_CHARS


# Intake


async def test_new_incident_posts_placeholder_and_dedupes(store):
    _, intake, _, slack, _, _ = make(store)
    assert await intake.handle(pd_event()) == "claimed"
    assert await intake.handle(pd_event()) == "duplicate"
    assert len(slack.posts) == 1 and slack.posts[0]["channel"] == "#vigil-test"
    assert "investigating" in json.dumps(slack.posts[0]["blocks"])
    inv = await store.get_investigation(1)
    assert inv["status"] == "queued" and inv["grouping_key"] == "oci-cost|KubeJobFailed"
    assert "primary-cluster" in inv["context"]["gaps"][0]


@respx.mock
async def test_pd_labels_are_fetched_and_routed(store):
    respx.get("https://pd.test/incidents/Q2K8TESTINC01/alerts").mock(
        return_value=httpx.Response(200, json=json.loads((FIX / "pd" / "incident_alerts.json").read_text()))
    )
    _, intake, _, _, _, _ = make(store, pd_key="k")
    assert await intake.handle(pd_event()) == "claimed"
    inv = await store.get_investigation(1)
    assert inv["context"]["namespace"] == "oci-cost" and inv["context"]["gaps"] == []


@respx.mock
async def test_pd_api_failure_retries_then_dead_letters_with_health_note(store):
    respx.get("https://pd.test/incidents/Q2K8TESTINC01/alerts").mock(return_value=httpx.Response(503))
    s, _, _, slack, runtime, _ = make(store, pd_key="k", intake_max_attempts=2)
    ev = pd_event()
    await store.inbox_put(ev.event_id, ev.incident_id, ev.source, ev.model_dump(mode="json"))
    await runtime.drain_inbox_once()
    assert (await store.fetchone("SELECT status FROM inbox"))["status"] == "pending"
    assert await store.get_investigation(1) is None
    await store.execute("UPDATE inbox SET next_attempt_at = now()")
    await runtime.drain_inbox_once()
    assert (await store.fetchone("SELECT status FROM inbox"))["status"] == "dead"
    assert slack.posts[-1]["channel"] == s.slack_health_channel and "gave up" in slack.posts[-1]["text"]


async def test_gates(store):
    _, intake, _, slack, _, flags = make(store)
    assert await intake.handle(parse_alertmanager(am_payload(namespace="jenkins"))) == "service_not_enabled"
    assert await intake.handle(parse_alertmanager(am_payload(namespace="other"))) == "unmapped_service"
    assert await intake.handle(parse_alertmanager(am_payload(cluster="eu-1"))) == "other_cluster"
    flags._value = Flags(disabled_services=["oci-cost"])
    assert await intake.handle(parse_alertmanager(am_payload(fp="f2"))) == "kill_switch"
    flags._value = Flags(enabled=False)
    assert await intake.handle(parse_alertmanager(am_payload(fp="f3"))) == "kill_switch"
    assert slack.posts == []


async def test_storm_grouping_and_status_updates(store):
    _, intake, _, slack, _, _ = make(store)
    await intake.handle(pd_event(event_id="a1", incident="INC1"))
    assert await intake.handle(pd_event(event_id="b1", incident="INC2")) == "attached"
    parent = slack.posts[0]["ts"]
    assert "grouped" in slack.thread(parent)[0]["text"]
    assert await intake.handle(pd_event("incident.acknowledged", "a2", "INC1")) == "acknowledged"
    assert "acknowledged" in slack.thread(parent)[-1]["text"]
    assert await intake.handle(pd_event("incident.resolved", "a3", "INC1")) == "resolved"
    assert not (await store.get_investigation(1))["resolved"]  # INC2 still open
    assert await intake.handle(pd_event("incident.resolved", "b3", "INC2")) == "resolved"
    inv = await store.get_investigation(1)
    assert inv["resolved"] and inv["status"] == "cancelled"
    assert all(u["ts"] == parent for u in slack.updates)
    assert await intake.handle(pd_event("incident.acknowledged", "zz", "NEVER-SEEN")) == "unknown_incident"


async def test_reopened_incident_starts_new_investigation_in_same_thread(store):
    _, intake, _, slack, _, _ = make(store)
    await intake.handle(pd_event(event_id="a1", incident="INC1"))
    await intake.handle(pd_event("incident.resolved", "a2", "INC1"))
    assert await intake.handle(pd_event("incident.reopened", "a3", "INC1")) == "reopened"
    new = await store.get_investigation(2)
    assert new["slack_ts"] == slack.posts[0]["ts"] and new["status"] == "queued"
    assert len([p for p in slack.posts if p["thread_ts"] is None]) == 1


# Pre-fetch


def test_plan_uses_context_pack_and_fallbacks():
    entry = Catalog.from_dict(CATALOG).by_name("oci-cost")
    p = plan(alert_name="KubeJobFailed", labels={"namespace": "oci-cost"}, entry=entry)
    assert p["metric_queries"] == ['kube_job_failed{namespace="oci-cost"}'] and p["runbook"] == "README.md"
    q = plan(alert_name="Other", labels={"namespace": "oci-cost", "pod": "p1"}, entry=entry)
    assert q["metric_queries"] == [] and q["log_query"].startswith('{namespace="oci-cost", pod="p1"}')
    u = plan(alert_name="X", labels={}, entry=None)
    assert u["namespaces"] == [] and u["log_query"] is None


async def test_prefetch_runs_in_parallel_and_records_gaps():
    from vigil_agent.agent.loop import Evidence

    gw = FakeGateway(fail={"search_logs"}, slow={"k8s_events"})
    p = plan(
        alert_name="KubeJobFailed",
        labels={"namespace": "oci-cost"},
        entry=Catalog.from_dict(CATALOG).by_name("oci-cost"),
    )

    async def similar():
        return [{"verdict": "last time: bad chart"}]

    ev = Evidence()
    out = await prefetch(p, lambda n, a: gw.call(n, a, None), similar, ev, budget_seconds=0.5)
    assert out["alert_rule"]["name"] == "KubeJobFailed"
    assert out["timeline"]["entries"][0]["summary"] == "bump collector"
    assert out["similar"] == [{"verdict": "last time: bad chart"}]
    assert any(g.startswith("logs:") for g in out["gaps"])
    assert any(g.startswith("events oci-cost: not finished") for g in out["gaps"])
    assert out["metrics"][0]["evidence_id"].startswith("E")


# Investigations


async def run_one(runtime, store):
    ids = await runtime.schedule_once()
    await asyncio.gather(*runtime.tasks.values())
    return ids


async def test_degraded_mode_posts_evidence_only(store):
    _, intake, _, slack, runtime, _ = make(store, model=None)
    await intake.handle(pd_event())
    assert await run_one(runtime, store) == [1]
    reply = slack.thread(slack.posts[0]["ts"])[0]
    text = json.dumps(reply["blocks"])
    assert "Evidence (no model analysis)" in text and "bump collector" in text
    inv = await store.get_investigation(1)
    assert inv["status"] == "done" and inv["step"] == "fast_done"


async def test_fast_then_deep_pass(store):
    deep = report_args(
        "deep",
        verdict="Chart bump broke the collector",
        hypotheses=[
            {
                "cause": "chart bump",
                "category": "bad_deploy",
                "confidence": "high",
                "evidence_ids": ["E1"],
                "status": "confirmed",
                "note": "diff shows new env var",
            }
        ],
    )
    model = FakeModel(
        [
            msg(tool_use("submit_report", report_args())),
            msg(tool_use("query_range", {"query": "up"})),
            msg(tool_use("submit_report", deep)),
        ]
    )
    _, intake, _, slack, runtime, _ = make(store, model=model)
    await intake.handle(pd_event())
    await run_one(runtime, store)
    parent = slack.posts[0]["ts"]
    replies = slack.thread(parent)
    assert [json.dumps(r["blocks"]).count("First pass") > 0 for r in replies][:1] == [True]
    assert "Deep pass" in json.dumps(replies[1]["blocks"]) and "confirmed" in json.dumps(replies[1]["blocks"])
    first_edit = [u for u in slack.updates if u["ts"] == replies[0]["ts"]]
    assert first_edit and "updated by the deep pass" in json.dumps(first_edit[0]["blocks"])
    assert any("Chart bump broke the collector" in json.dumps(u["blocks"]) for u in slack.updates if u["ts"] == parent)
    inv = await store.get_investigation(1)
    assert inv["status"] == "done" and inv["step"] == "deep_done" and inv["deep_report"]["mode"] == "deep"
    kinds = {r["kind"] for r in await store.fetchall("SELECT kind FROM transcripts")}
    assert {"model_request", "model_response", "tool_call"} <= kinds
    prefetched = await store.fetchall("SELECT payload FROM transcripts WHERE pass = 'prefetch' AND kind = 'tool_call'")
    assert prefetched and all({"tool", "args", "result"} <= set(r["payload"]) for r in prefetched)
    assert float(inv["cost_usd"]) > 0


async def test_fast_pass_missing_report_falls_back_to_evidence(store):
    model = FakeModel(
        [
            msg({"type": "text", "text": "hmm"}),
            msg({"type": "text", "text": "hmm"}),
            msg(tool_use("submit_report", report_args("deep"))),
        ]
    )
    _, intake, _, slack, runtime, _ = make(store, model=model)
    await intake.handle(pd_event())
    await run_one(runtime, store)
    first = slack.thread(slack.posts[0]["ts"])[0]
    assert "Evidence (no model analysis)" in json.dumps(first["blocks"])
    assert "without a valid report" in json.dumps(first["blocks"])


async def test_resolve_stops_deep_pass(store):
    _, intake, _, slack, runtime, _ = make(store)

    async def resolve_then_tool(messages):
        await intake.handle(pd_event("incident.resolved", "r1"))
        return msg(tool_use("query_range", {"query": "up"}))

    model = FakeModel([msg(tool_use("submit_report", report_args())), resolve_then_tool])
    runtime.investigator.model = model
    runtime.investigator.s.model_enabled = True
    await intake.handle(pd_event())
    await run_one(runtime, store)
    texts = [p["text"] for p in slack.thread(slack.posts[0]["ts"])]
    assert any("Resolved during the deep pass" in t for t in texts)
    assert len(model.requests) == 2


async def test_lease_takeover_resumes_without_reposting_first_pass(store):
    model = FakeModel(
        [msg(tool_use("submit_report", report_args())), msg(tool_use("submit_report", report_args("deep")))]
    )
    _, intake, inv_a, slack, runtime, _ = make(store, model=model)
    await intake.handle(pd_event())
    await store.schedule("pod-a", 3, 5, 90, True)
    inv_a.owner = "pod-a"
    # Pod A completes the fast pass, then dies before the deep pass.
    model_a_script = model.script
    model.script = [model_a_script.pop(0)]
    original = inv_a._deep

    async def die(*a, **k):
        raise asyncio.CancelledError

    inv_a._deep = die
    with pytest.raises(asyncio.CancelledError):
        await inv_a.run(1)
    inv_a._deep = original
    assert (await store.get_investigation(1))["step"] == "fast_done"
    await store.execute("UPDATE investigations SET lease_expires_at = now() - interval '1 second'")
    model.script = model_a_script
    runtime.investigator.owner = runtime.s.owner = "pod-b"
    await run_one(runtime, store)
    labels = [json.dumps(p["blocks"]) for p in slack.thread(slack.posts[0]["ts"])]
    assert sum("First pass" in t for t in labels) == 1 and any("Deep pass" in t for t in labels)
    assert (await store.get_investigation(1))["lease_owner"] is None


async def test_spend_cap_switches_to_evidence_only(store):
    model = FakeModel([])
    _, intake, _, slack, runtime, _ = make(store, model=model, daily_spend_cap_usd=1.0)
    await store.add_spend("investigation", 2.0)
    await intake.handle(pd_event())
    await run_one(runtime, store)
    assert "spend cap" in json.dumps(slack.thread(slack.posts[0]["ts"])[0]["blocks"])
    assert model.requests == []


# Early start


async def test_early_start_held_then_released_by_page(store):
    model = FakeModel(
        [msg(tool_use("submit_report", report_args())), msg(tool_use("submit_report", report_args("deep")))]
    )
    _, intake, _, slack, runtime, _ = make(
        store, model=model, early_start_enabled=True, pending_alerts_url="http://prom"
    )
    pending = {
        "data": {
            "alerts": [
                {
                    "state": "pending",
                    "activeAt": "2026-10-20T06:00:00Z",
                    "labels": {"alertname": "KubeJobFailed", "namespace": "oci-cost"},
                    "annotations": {"summary": "Job failed"},
                }
            ]
        }
    }
    with respx.mock:
        respx.get("http://prom/api/v1/alerts").mock(return_value=httpx.Response(200, json=pending))
        results = await asyncio.gather(runtime.poll_pending_once(), runtime.poll_pending_once())
    assert sum(results) == 1
    await run_one(runtime, store)
    # The losing concurrent insert also takes a sequence value, so look the ID up.
    inv_id = (await store.fetchone("SELECT id FROM investigations"))["id"]
    inv = await store.get_investigation(inv_id)
    assert inv["status"] == "held" and inv["fast_report"] and slack.posts == []
    assert len(model.requests) == 1  # no deep pass while held
    assert await intake.handle(pd_event()) == "early_start_released"
    replies = slack.thread(slack.posts[0]["ts"])
    assert "early start" in json.dumps(replies[0]["blocks"])
    await run_one(runtime, store)
    assert (await store.get_investigation(inv_id))["step"] == "deep_done"
    assert sum("First pass" in json.dumps(p["blocks"]) for p in slack.thread(slack.posts[0]["ts"])) == 1


async def test_early_start_pauses_at_half_the_spend_cap(store):
    _, _, _, _, runtime, _ = make(
        store, model=FakeModel([]), early_start_enabled=True, pending_alerts_url="http://prom", daily_spend_cap_usd=10
    )
    await store.add_spend("investigation", 6)
    assert not await runtime.allow_early()


async def test_flapping_pending_alert_is_skipped(store):
    _, _, _, _, runtime, _ = make(
        store, model=FakeModel([]), early_start_enabled=True, pending_alerts_url="http://prom"
    )
    for _ in range(4):
        await store.incr_flap("oci-cost|KubeJobFailed")
    pending = {
        "data": {
            "alerts": [
                {"state": "pending", "activeAt": "t", "labels": {"alertname": "KubeJobFailed", "namespace": "oci-cost"}}
            ]
        }
    }
    with respx.mock:
        respx.get("http://prom/api/v1/alerts").mock(return_value=httpx.Response(200, json=pending))
        assert await runtime.poll_pending_once() == 0


async def test_flap_is_counted_when_pending_goes_inactive(store):
    _, _, _, _, runtime, _ = make(
        store, model=FakeModel([]), early_start_enabled=False, pending_alerts_url="http://prom"
    )
    pending = {
        "data": {
            "alerts": [
                {"state": "pending", "activeAt": "t", "labels": {"alertname": "KubeJobFailed", "namespace": "oci-cost"}}
            ]
        }
    }
    with respx.mock:
        route_ = respx.get("http://prom/api/v1/alerts")
        route_.mock(return_value=httpx.Response(200, json=pending))
        await runtime.poll_pending_once()
        route_.mock(return_value=httpx.Response(200, json={"data": {"alerts": []}}))
        await runtime.poll_pending_once()
    assert await store.flap_count("oci-cost|KubeJobFailed") == 1


async def test_ack_after_deep_pass_still_updates_thread(store):
    _, intake, _, slack, runtime, _ = make(store)
    await intake.handle(pd_event())
    await run_one(runtime, store)
    before = len(slack.posts)
    assert await intake.handle(pd_event("incident.acknowledged", "ack1")) == "acknowledged"
    assert len(slack.posts) == before + 1


async def test_feedback_buttons(store):
    from vigil_agent.slack.interactivity import handle_action

    _, intake, _, _, runtime, _ = make(store)
    await intake.handle(pd_event())
    await run_one(runtime, store)
    assert await handle_action(store, {"action_id": "vigil_helpful", "value": "1"}, "U1") == "helpful"
    assert await handle_action(store, {"action_id": "vigil_reinvestigate", "value": "1"}, "U1") == "reinvestigate"
    inv = await store.get_investigation(1)
    assert inv["status"] == "queued" and inv["step"] == "fast_done"
    assert len(await store.fetchall("SELECT * FROM feedback")) == 2


async def test_early_start_without_model_releases_evidence(store):
    _, intake, _, slack, runtime, _ = make(
        store, model=None, early_start_enabled=True, pending_alerts_url="http://prom"
    )
    await store.add_spend("investigation", 0)
    pending = {
        "data": {
            "alerts": [
                {"state": "pending", "activeAt": "t", "labels": {"alertname": "KubeJobFailed", "namespace": "oci-cost"}}
            ]
        }
    }
    with respx.mock:
        respx.get("http://prom/api/v1/alerts").mock(return_value=httpx.Response(200, json=pending))
        assert await runtime.poll_pending_once() == 1
    await run_one(runtime, store)
    assert (await store.get_investigation(1))["status"] == "held" and slack.posts == []
    assert await intake.handle(pd_event()) == "early_start_released"
    first = slack.thread(slack.posts[0]["ts"])[0]
    assert "Evidence (no model analysis)" in json.dumps(first["blocks"])
    await run_one(runtime, store)
    assert (await store.get_investigation(1))["status"] == "done"
    assert len(slack.thread(slack.posts[0]["ts"])) == 1


async def test_namespace_is_preferred_over_the_pagerduty_service(store):
    from vigil_agent.catalog import Catalog

    _, intake, _, _, _, _ = make(store)
    cat = Catalog.from_dict(
        {
            "services": [
                {
                    "name": "cluster",
                    "vigil_enabled": True,
                    "pd_service_ids": ["PSVC123"],
                    "team": "t",
                    "slack_channel": "#c",
                },
                {
                    "name": "oci-cost",
                    "vigil_enabled": True,
                    "team": "t",
                    "slack_channel": "#c",
                    "namespaces": ["oci-cost"],
                },
            ]
        }
    )
    intake.catalog_file._value = cat
    ns = pd_event()
    ns.alerts = [Alert(labels={"namespace": "oci-cost"})]
    assert intake.resolve_service(ns).name == "oci-cost"
    node = pd_event()
    node.alerts = [Alert(labels={"node": "n1"})]
    assert intake.resolve_service(node).name == "cluster"


async def test_redelivery_after_a_stop_before_the_placeholder_posts_it(store):
    _, intake, _, slack, _, _ = make(store)
    ev = pd_event()
    original = intake.pub.ensure_parent

    async def stop(*a, **k):
        raise asyncio.CancelledError

    intake.pub.ensure_parent = stop
    with pytest.raises(asyncio.CancelledError):
        await intake.handle(ev)
    intake.pub.ensure_parent = original
    assert slack.posts == []
    assert await intake.handle(ev) == "already_attached"
    assert len(slack.posts) == 1 and slack.posts[0]["thread_ts"] is None


async def test_parent_uses_channel_id_and_a_failed_edit_does_not_fail(store):
    _, intake, _, slack, runtime, _ = make(store)
    slack.channel_ids = {"#vigil-test": "C999"}

    async def broken_update(*a, **k):
        raise RuntimeError("channel_not_found")

    slack.update = broken_update
    await intake.handle(pd_event())
    assert (await store.get_investigation(1))["slack_channel"] == "C999"
    await run_one(runtime, store)
    assert (await store.get_investigation(1))["status"] == "done"


async def test_prefetch_reads_the_failed_build_of_a_jenkins_alert():
    from vigil_agent.agent.loop import Evidence

    gw = FakeGateway()
    p = plan(alert_name="JenkinsJobFailed", labels={"namespace": "jenkins", "jenkins_job": "tts"}, entry=None)

    async def similar():
        return []

    out = await prefetch(p, lambda n, a: gw.call(n, a, None), similar, Evidence(), budget_seconds=2)
    assert ("jenkins_build_log", {"job": "tts", "build": "lastFailedBuild", "tail_lines": 150}) in gw.calls
    assert "jenkins" in out and out["jenkins"]["evidence_id"].startswith("E")
