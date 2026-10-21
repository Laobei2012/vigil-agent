"""Scenario runner: the fast and deep passes against a recorded world, several runs, a pass rate.

A scenario is YAML (see evals/scenarios). The runner uses the same pre-fetch,
prompts, and loop as production; only the tool gateway is replaced by the
recorded world. The model is real (Bedrock) unless a test passes a fake.

Usage: vigil-agent eval evals/scenarios [--runs N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vigil_agent.agent.loop import Budget, Evidence
from vigil_agent.agent.model import ModelClient
from vigil_agent.catalog import Catalog, ReloadingFile
from vigil_agent.config import Settings
from vigil_agent.evals.assertions import check
from vigil_agent.evals.world import World
from vigil_agent.orchestrator.investigation import Investigator
from vigil_agent.orchestrator.prefetch import plan, prefetch


class _EvalStore:
    """The parts of the store that a pass touches: transcripts and the resolve check."""

    def __init__(self):
        self.transcript: list[tuple[str, str]] = []

    async def add_transcript(self, inv_id, pass_, kind, payload):
        self.transcript.append((pass_, kind))

    async def get_investigation(self, inv_id):
        return {"resolved": False, "status": "running", "lease_owner": "eval"}


@dataclass
class RunResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    fast_seconds: float = 0.0
    tool_calls: int = 0
    unrecorded: int = 0


@dataclass
class ScenarioResult:
    name: str
    runs: list[RunResult]
    pass_rate: float
    required: float

    @property
    def passed(self) -> bool:
        return self.pass_rate >= self.required


async def run_once(scenario: dict, base: Path, model: ModelClient, settings: Settings, catalog: Catalog) -> RunResult:
    world = World.load(base / scenario["world"])
    alert = json.loads((base / scenario["alert"]).read_text())
    labels = alert["alerts"][0]["labels"]
    entry = catalog.by_namespace(labels.get("namespace"))
    service = entry.name if entry else f"unmapped:{labels.get('namespace') or 'none'}"
    inv = {
        "id": 0,
        "service": service,
        "alert_name": labels.get("alertname", ""),
        "lease_owner": "eval",
        "context": {
            "title": alert.get("title", labels.get("alertname")),
            "urgency": "high",
            "alerts": alert["alerts"],
            "incident_id": "eval",
        },
    }
    investigator = Investigator(
        settings, _EvalStore(), ReloadingFile(None, None, catalog), world, model, publisher=None, pd=None, owner="eval"
    )
    evidence = Evidence()
    budget = Budget(tokens=settings.investigation_token_budget)

    async def no_similar():
        return scenario.get("similar", [])

    pre = await prefetch(
        plan(alert_name=inv["alert_name"], labels=labels, entry=entry),
        world.call,
        no_similar,
        evidence,
        settings.prefetch_timeout_seconds,
    )
    expect = scenario.get("expect", {})
    failures: list[str] = []
    started = time.monotonic()
    fast = await investigator._fast(inv, pre, evidence, budget)
    fast_seconds = time.monotonic() - started
    failures += [
        f"fast: {f}"
        for f in check(
            expect.get("fast_pass", {}),
            fast.report,
            seconds=fast_seconds,
            tool_calls=fast.tool_calls,
            evidence=evidence.items,
        )
    ]
    tool_calls = fast.tool_calls
    if "deep_pass" in expect:
        deep = await investigator._deep(inv, pre, fast.report, evidence, budget)
        tool_calls += deep.tool_calls
        failures += [
            f"deep: {f}"
            for f in check(
                expect["deep_pass"], deep.report, seconds=0, tool_calls=deep.tool_calls, evidence=evidence.items
            )
        ]
    return RunResult(not failures, failures, fast_seconds, tool_calls, len(world.misses))


async def run_scenario(
    path: Path, model: ModelClient, settings: Settings, catalog: Catalog, runs: int | None = None
) -> ScenarioResult:
    scenario = yaml.safe_load(path.read_text())
    n = runs or scenario.get("runs", 1)
    results = [await run_once(scenario, path.parent, model, settings, catalog) for _ in range(n)]
    rate = sum(r.passed for r in results) / len(results)
    return ScenarioResult(scenario["scenario"], results, rate, scenario.get("pass_rate", 1.0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vigil-agent eval")
    parser.add_argument("path", help="a scenario file or a directory of scenarios")
    parser.add_argument("--runs", type=int, default=None)
    parser.add_argument("--catalog", default="catalog/services.yaml")
    args = parser.parse_args(argv)
    from vigil_agent.agent.model import build_model

    settings = Settings()
    model = build_model(settings)
    catalog = Catalog.load(args.catalog)
    p = Path(args.path)
    files = sorted(p.glob("*.yaml")) if p.is_dir() else [p]

    async def go() -> int:
        failed = 0
        for f in files:
            r = await run_scenario(f, model, settings, catalog, args.runs)
            print(f"{'PASS' if r.passed else 'FAIL'} {r.name}: {r.pass_rate:.0%} (need {r.required:.0%})")
            for i, run in enumerate(r.runs):
                for failure in run.failures:
                    print(f"    run {i + 1}: {failure}")
            failed += not r.passed
        return 1 if failed else 0

    return asyncio.run(go())
