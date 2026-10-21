"""Send Alertmanager webhook payloads to a running orchestrator.

The payloads have the exact shape of Alertmanager's webhook_configs, so the
test exercises the same intake path as real alerts without firing an alert.

Usage: uv run python scripts/e2e_alertmanager.py <base-url> <scenario> [status]
Scenarios: crashloop, crashloop-second-pod, jobfailed, node, other-cluster, health
"""

import hashlib
import json
import sys
from datetime import UTC, datetime

import httpx

CLUSTER = "oci-phx"


def alert(labels: dict, annotations: dict, status: str) -> dict:
    fp = hashlib.sha256(json.dumps(labels, sort_keys=True).encode()).hexdigest()[:16]
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "status": status,
        "labels": labels,
        "annotations": annotations,
        "startsAt": now,
        "endsAt": now if status == "resolved" else "0001-01-01T00:00:00Z",
        "generatorURL": "https://monitoring.oci.liwenxin.com/alerting/list",
        "fingerprint": fp,
    }


def group(alerts: list[dict], group_labels: dict, status: str) -> dict:
    common = {k: v for k, v in alerts[0]["labels"].items() if all(a["labels"].get(k) == v for a in alerts)}
    key = '{}/{severity="warning"}:' + "{" + ", ".join(f'{k}="{v}"' for k, v in sorted(group_labels.items())) + "}"
    return {
        "version": "4",
        "groupKey": key,
        "truncatedAlerts": 0,
        "status": status,
        "receiver": "vigil-agent",
        "groupLabels": group_labels,
        "commonLabels": common,
        "commonAnnotations": alerts[0]["annotations"] if len(alerts) == 1 else {},
        "externalURL": "http://kube-prometheus-stack-alertmanager.monitoring:9093",
        "alerts": alerts,
    }


def scenario(name: str, status: str) -> dict:
    if name in ("crashloop", "crashloop-second-pod"):
        pod = "payments-api" if name == "crashloop" else "payments-worker"
        labels = {
            "alertname": "KubePodCrashLooping",
            "namespace": "vigil-chaos",
            "pod": pod,
            "container": "api",
            "severity": "warning",
            "cluster": CLUSTER,
            "job": "kube-state-metrics",
            "reason": "CrashLoopBackOff",
        }
        ann = {
            "summary": "Pod is crash looping.",
            "description": f'Pod vigil-chaos/{pod} (api) is in waiting state (reason: "CrashLoopBackOff").',
            "runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubepodcrashlooping",
        }
        return group(
            [alert(labels, ann, status)], {"alertname": labels["alertname"], "namespace": "vigil-chaos"}, status
        )
    if name == "jobfailed":
        labels = {
            "alertname": "KubeJobFailed",
            "namespace": "oci-cost",
            "job_name": "oci-cost-collector-a-29999",
            "severity": "warning",
            "cluster": CLUSTER,
            "condition": "true",
        }
        ann = {
            "summary": "Job failed to complete.",
            "description": "Job oci-cost/oci-cost-collector-a-29999 failed to complete. Removing failed job "
            "after investigation should clear this alert.",
        }
        return group([alert(labels, ann, status)], {"alertname": "KubeJobFailed", "namespace": "oci-cost"}, status)
    if name == "node":
        labels = {"alertname": "KubeNodeNotReady", "node": "10.1.1.250", "severity": "warning", "cluster": CLUSTER}
        ann = {"summary": "Node is not ready.", "description": "10.1.1.250 has been unready for more than 15 minutes."}
        return group([alert(labels, ann, status)], {"alertname": "KubeNodeNotReady"}, status)
    if name == "other-cluster":
        labels = {"alertname": "KubeJobFailed", "namespace": "oci-cost", "severity": "warning", "cluster": "eu-1"}
        return group([alert(labels, {"summary": "x"}, status)], {"alertname": "KubeJobFailed"}, status)
    if name == "health":
        labels = {
            "alertname": "VigilIntakeDeadLetters",
            "namespace": "vigil-agent",
            "severity": "warning",
            "cluster": CLUSTER,
            "vigil_health": "true",
        }
        return group([alert(labels, {"summary": "x"}, status)], {"alertname": "VigilIntakeDeadLetters"}, status)
    raise SystemExit(f"unknown scenario {name}")


def main() -> None:
    base, name = sys.argv[1], sys.argv[2]
    status = sys.argv[3] if len(sys.argv) > 3 else "firing"
    r = httpx.post(f"{base.rstrip('/')}/webhooks/alertmanager", json=scenario(name, status), timeout=10)
    print(name, status, r.status_code, r.text)


if __name__ == "__main__":
    main()
