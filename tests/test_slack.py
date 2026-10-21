import json

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from vigil_agent.agent.report import Hypothesis, Mitigation, Report
from vigil_agent.slack.client import SlackClient, post_blocks
from vigil_agent.slack.render import (
    BLOCKS_PER_MESSAGE,
    SECTION_LIMIT,
    SUGGESTED,
    evidence_only_blocks,
    report_blocks,
    sanitize,
    split_messages,
)

ALLOWED = ["oci.liwenxin.com", "github.com"]


def report(**kw) -> Report:
    base = dict(
        mode="fast",
        verdict="bad deploy",
        summary="5xx on checkout",
        impact="checkout down",
        timeline=["10:00 deploy"],
        confidence="medium",
        hypotheses=[
            Hypothesis(cause="bad image", category="bad_deploy", confidence="high", evidence_ids=["E1"]),
            Hypothesis(cause="db", category="dependency_failure", confidence="low", evidence_ids=["E9"]),
        ],
        mitigations=[
            Mitigation(action="roll back", risk="low", expected_effect="5xx stop", command="flux suspend hr x")
        ],
        gaps=["no logs"],
    )
    base.update(kw)
    return Report(**base)


def test_mentions_are_removed():
    out = sanitize("hey <!channel> <!here|here> <@U123ABC> @here @channel <!subteam^S1|team>", ALLOWED)
    for bad in ("<!channel>", "<!here", "<@U123ABC>", "@here", "@channel", "<!subteam"):
        assert bad not in out


def test_links_outside_allowlist_become_plain_text():
    out = sanitize(
        "see <https://evil.example/x|click> and https://evil.example/y and "
        "https://monitoring.oci.liwenxin.com/d and <https://github.com/a|gh>",
        ALLOWED,
    )
    assert "<https://evil.example" not in out and "https://evil.example/y" not in out
    assert "evil.example" in out  # kept as defanged text
    assert "https://monitoring.oci.liwenxin.com/d" in out and "<https://github.com/a|gh>" in out


def test_report_renders_expected_sections():
    evidence = {"E1": {"url": "https://monitoring.oci.liwenxin.com/explore", "title": "5xx rate"}}
    blocks = report_blocks(
        report(), evidence, allowed=ALLOWED, footer="model x", label="First pass", investigation_id=7
    )
    text = json.dumps(blocks)
    for part in (
        "Summary",
        "Likely cause",
        "Suggested mitigations",
        SUGGESTED,
        "Evidence",
        "Gaps",
        "model x",
        "vigil_helpful",
    ):
        assert part in text
    assert "unsupported: no evidence cited" in text  # E9 was never seen
    assert "<https://monitoring.oci.liwenxin.com/explore|E1>" in text


def test_long_report_splits_within_slack_limits():
    big = report(summary="x" * 9000, timeline=[f"event {i} " + "y" * 200 for i in range(800)])
    blocks = report_blocks(big, {}, allowed=ALLOWED, footer="f", label="Deep pass", investigation_id=1)
    for b in blocks:
        if b["type"] == "section":
            assert len(b["text"]["text"]) <= SECTION_LIMIT
    parts = split_messages(blocks)
    assert len(parts) > 1 and all(len(p) <= BLOCKS_PER_MESSAGE for p in parts)


def test_evidence_only_reply():
    prefetch = {
        "alert_rule": {
            "name": "KubeJobFailed",
            "query": "kube_job_failed > 0",
            "annotations": {"description": "Job failed <!channel>"},
        },
        "timeline": {"entries": [{"time": "t", "source": "github_pr", "summary": "bump chart"}]},
        "events": [],
        "similar": [{"investigation": 3, "at": "2026-10-20 06:00", "verdict": None}],
        "gaps": ["logs: timeout"],
        "runbook": "README.md#jobs",
    }
    prefetch["metrics"] = [
        {
            "query": "kube_job_failed",
            "evidence_id": "E1",
            "series": [{"metric": {"job_name": "j1"}, "last": [1, "1"], "max": 1.0}],
        }
    ]
    prefetch["logs"] = {
        "evidence_id": "E2",
        "hits": 4,
        "top_patterns": [{"count": 2, "example": "panic: cannot connect to database"}],
    }
    alerts = [
        {
            "labels": {"alertname": "KubeJobFailed", "namespace": "oci-cost", "job_name": "j1"},
            "annotations": {"description": "Job oci-cost/j1 failed to complete."},
        }
    ]
    text = json.dumps(
        evidence_only_blocks(prefetch, {}, allowed=ALLOWED, reason="model off", footer="f", alerts=alerts)
    )
    assert "KubeJobFailed" in text and "bump chart" in text and "logs: timeout" in text
    assert "Job oci-cost/j1 failed to complete." in text and "job_name=j1" in text
    assert "investigation 3 on 2026-10-20 06:00: evidence only" in text
    assert "last 1, max 1" in text and "2x" in text and "panic: cannot connect to database" in text
    assert "<!channel>" not in text


class FakeWeb:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    async def chat_postMessage(self, **kw):  # noqa: N802 - Slack SDK name
        self.calls += 1
        if self.failures:
            status, headers = self.failures.pop(0)
            resp = AsyncSlackResponse(
                client=None,
                http_verb="POST",
                api_url="u",
                req_args={},
                data={"ok": False, "error": "ratelimited"},
                headers=headers,
                status_code=status,
            )
            raise SlackApiError("fail", resp)
        return {"ts": "1.0"}


async def test_429_is_retried_after_retry_after():
    slept = []

    async def sleep(s):
        slept.append(s)

    web = FakeWeb([(429, {"Retry-After": "2"})])
    client = SlackClient("x", client=web, sleep=sleep)
    assert await client.post("#c", "t") == "1.0"
    assert web.calls == 2 and slept == [2.0]


async def test_4xx_other_than_429_is_not_retried():
    web = FakeWeb([(400, {})])
    with pytest.raises(SlackApiError):
        await SlackClient("x", client=web).post("#c", "t")
    assert web.calls == 1


async def test_post_blocks_posts_each_part():
    posted = []

    class P:
        async def post(self, channel, text, blocks=None, thread_ts=None):
            posted.append(len(blocks))
            return str(len(posted))

    ts = await post_blocks(P(), "#c", "t", [{"type": "divider"}] * 120, "1.0")
    assert posted == [50, 50, 20] and ts == ["1", "2", "3"]


async def test_channel_id_is_kept_for_updates():
    class Web:
        async def chat_postMessage(self, **kw):  # noqa: N802 - Slack SDK name
            return {"ts": "1.0", "channel": "C123"}

    client = SlackClient("x", client=Web())
    await client.post("#alertmanager", "t")
    assert client.channel_ids == {"#alertmanager": "C123"}
