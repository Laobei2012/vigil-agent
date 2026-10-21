# vigil-agent

Vigil is an on-call agent. It receives PagerDuty and Alertmanager alerts, investigates them with read-only tools, and posts a triage summary with suggested mitigations to one Slack thread per incident. Humans act on the suggestions. The agent never changes production state.

This repository holds phase 1 of the design: intake, the tool gateway, the two-pass investigation (fast pass and deep pass) with Claude Opus on Amazon Bedrock, Slack output, and the Helm chart.

## Components

| Component | Command | Purpose |
| --- | --- | --- |
| Orchestrator | `vigil-agent orchestrator` | Webhook receiver (test cluster), intake, scheduler, investigations, Slack |
| Tool gateway | `vigil-agent gateway` | The only process with backend credentials. It serves the allowlisted read-only tools. |
| Receiver | `vigil-agent receiver` | Production webhook receiver that writes to SQS |

The tool allowlist is `src/vigil_agent/gateway/allowlist.yaml`. It is part of the image, so a new tool needs a reviewed pull request and a new signed image.

## Development

You need Python 3.12, uv, ripgrep, and a PostgreSQL for the state store tests.

```sh
docker run -d --name vigil-pg -e POSTGRES_USER=vigil -e POSTGRES_PASSWORD=vigil -e POSTGRES_DB=vigil -p 55432:5432 postgres:18
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
helm lint deploy/helm/vigil-agent
```

No test calls Bedrock. The tests use recorded model responses (`tests/fakes.py`).

## Configuration

The orchestrator reads environment variables with the prefix `VIGIL_` (`src/vigil_agent/config.py`). The gateway reads the prefix `VIGIL_GW_` (`src/vigil_agent/gateway/settings.py`). These features stay off until their secret exists:

- The model. Set `VIGIL_MODEL_ENABLED=true` and give AWS credentials that can call the pinned model. Without the model, each incident gets the evidence-only reply.
- Slack. Without `VIGIL_SLACK_BOT_TOKEN`, or with `VIGIL_SLACK_DRY_RUN=true`, messages go to the `slack_outbox` table.
- PagerDuty. Without `VIGIL_PD_WEBHOOK_SECRETS`, the PagerDuty webhook returns 503. Without `VIGIL_PD_API_KEY`, incidents are routed by the primary-cluster rule.

## Kill switch

Edit the flags ConfigMap (`<release>-flags`). Set `enabled: false`, or list a cluster in `disabled_clusters` or a service in `disabled_services`. The orchestrator reads the file again without a restart.
