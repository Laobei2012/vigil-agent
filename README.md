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

- The model. Set `VIGIL_MODEL_ENABLED=true`. With `VIGIL_MODEL_PROVIDER=bedrock` (the default), give AWS credentials that can call the pinned model. With `VIGIL_MODEL_PROVIDER=anthropic`, give `ANTHROPIC_API_KEY` and, for a compatible endpoint, `ANTHROPIC_BASE_URL`. Without the model, each incident gets the evidence-only reply.
- Slack. Without `VIGIL_SLACK_BOT_TOKEN`, or with `VIGIL_SLACK_DRY_RUN=true`, messages go to the `slack_outbox` table.
- PagerDuty. Without `VIGIL_PD_WEBHOOK_SECRETS`, the PagerDuty webhook returns 503. Without `VIGIL_PD_API_KEY`, incidents are routed by the primary-cluster rule.

## Secrets

You need a secret only for the features that you turn on. PagerDuty is the primary intake: each paged alert reaches the agent through the PagerDuty webhook. Alerts that do not page (warnings) come directly from the Alertmanager webhook and need no secret.

| Vault secret | Key in Secret `vigil-agent` | Environment variable | Needed for |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | `anthropic-api-key` | `ANTHROPIC_API_KEY` | Model through the Anthropic API |
| `ANTHROPIC_BASE_URL` | `anthropic-base-url` | `ANTHROPIC_BASE_URL` | An Anthropic-compatible endpoint |
| `VIGIL_AWS_ACCESS_KEY_ID`, `VIGIL_AWS_SECRET_ACCESS_KEY` | `aws-access-key-id`, `aws-secret-access-key` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Model on Bedrock, outside AWS |
| `VIGIL_SLACK_BOT_TOKEN` | `slack-bot-token` | `VIGIL_SLACK_BOT_TOKEN` | Posting to Slack |
| `VIGIL_SLACK_APP_TOKEN` | `slack-app-token` | `VIGIL_SLACK_APP_TOKEN` | Feedback buttons (Socket Mode) |
| `VIGIL_PD_WEBHOOK_SECRET` | `pd-webhook-secrets` | `VIGIL_PD_WEBHOOK_SECRETS` | PagerDuty webhook intake |
| `VIGIL_PD_API_KEY` | `pd-api-key` | `VIGIL_PD_API_KEY` | Alert labels of PagerDuty incidents |
| `VIGIL_GITHUB_TOKEN` | `github-token` | `VIGIL_GW_GITHUB_TOKEN` | Commits, PRs, and Terraform runs in the timeline |
| `VIGIL_AM_WEBHOOK_TOKEN` | `am-webhook-token` | `VIGIL_AM_WEBHOOK_TOKEN` | A bearer token on the Alertmanager webhook |

The gateway token needs no action. An External Secrets `Password` generator creates it.

### Put a value in OCI Vault

You need `kubectl` access to the cluster and an `oci` CLI profile that can manage secrets in the vault.

1. Run `scripts/vault-put.sh <kube-context> <VAULT_SECRET_NAME>`.
2. Paste the value at the hidden prompt.
3. Add the secret to the ExternalSecret `vigil-agent` in the GitOps repository (`gitops/tools/vigil-agent/secret.yaml`). Add one `data` entry and one `template.data` line.

The script finds the vault from the `oracle-vault` ClusterSecretStore. If the secret exists, the script writes a new version.

Add a secret to the ExternalSecret only after the secret exists in the vault. If one key is missing, the whole ExternalSecret fails, and the other keys stop syncing too.

### PagerDuty

PagerDuty is the primary intake for paged alerts. You need both values.

The webhook tells the agent that an incident exists, but it carries no alert labels. The API key reads those labels (`cluster`, `namespace`, `alertname`) back from PagerDuty. The agent needs them to route the incident and to pick the context pack.

1. In PagerDuty, open Integrations, then Generic Webhooks (v3), then New Webhook.
2. Set the URL to `https://vigil.<cluster domain>/webhooks/pagerduty`.
3. Set the scope type to Service, and select the service.
4. Select the events `incident.triggered`, `incident.acknowledged`, `incident.resolved`, and `incident.reopened`.
5. Click Add Webhook. Copy the signing secret now, because PagerDuty shows it only one time.
6. Store the signing secret as `VIGIL_PD_WEBHOOK_SECRET`.
7. Open Integrations, then API Access Keys, and create a key with Read-only API Key selected.
8. Store the key as `VIGIL_PD_API_KEY`.
9. In the HelmRelease values, set `httpRoute.enabled: true`. Add the PagerDuty service ID to `pd_service_ids` in the catalog.

### Anthropic API

1. Create an API key in the console of your provider.
2. Store it as `ANTHROPIC_API_KEY`.
3. If you do not use api.anthropic.com, store the endpoint as `ANTHROPIC_BASE_URL`.
4. In the HelmRelease values, set `config.modelEnabled: true`, `config.modelProvider: anthropic`, and `config.modelId`.

The endpoint receives the redacted evidence of each investigation. Before production data reaches the agent, make sure that this provider is acceptable for that data.

### Amazon Bedrock

1. In AWS, create an IAM user or role with only this policy. Replace the region, the account, and the inference profile ID.

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
       "Resource": [
         "arn:aws:bedrock:us-west-2:<account>:inference-profile/us.anthropic.claude-opus-5-5",
         "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5"
       ]
     }]
   }
   ```

2. Outside AWS, create an access key for the user. Store it as `VIGIL_AWS_ACCESS_KEY_ID` and `VIGIL_AWS_SECRET_ACCESS_KEY`. On EKS, use IRSA or EKS Pod Identity instead, and store no key.
3. Set `config.modelProvider: bedrock` and `config.modelId` to the inference profile ID.

### Slack

1. Open https://api.slack.com/apps and click Create New App, then From a manifest.
2. Select the workspace and paste this manifest.

   ```yaml
   display_information:
     name: vigil
     description: On-call triage agent. It reads and suggests; humans act.
   features:
     bot_user:
       display_name: vigil
       always_online: true
   oauth_config:
     scopes:
       bot: [chat:write]
   settings:
     interactivity:
       is_enabled: true
     socket_mode_enabled: true
   ```

3. Click Install to Workspace.
4. Copy the Bot User OAuth Token (`xoxb-...`) and store it as `VIGIL_SLACK_BOT_TOKEN`.
5. In Basic Information, under App-Level Tokens, generate a token with the scope `connections:write`.
6. Store that token (`xapp-...`) as `VIGIL_SLACK_APP_TOKEN`.
7. In Slack, invite the bot to the channel: `/invite @vigil`.
8. In the HelmRelease values, set `config.slackDryRun: false` and the channel names in `config` and in the catalog.

The bot needs no other scope. It posts only to channels that it is a member of.

### GitHub

1. Open https://github.com/settings/personal-access-tokens and click Generate new token (fine-grained).
2. Under Repository access, select only the repositories in the catalog.
3. Give read-only access to Contents, Pull requests, and Actions. Metadata is read-only by default.
4. Store the token as `VIGIL_GITHUB_TOKEN`.

In production, use a GitHub App with the same read permissions. Its installation tokens expire after one hour.

### Alertmanager webhook token

This token is optional. The webhook is reachable only inside the cluster.

1. Create a value: `openssl rand -hex 32`.
2. Store it as `VIGIL_AM_WEBHOOK_TOKEN`.
3. In the Alertmanager receiver `vigil-agent`, add `http_config.authorization.credentials_file` with a file that holds the same value.

## Kill switch

Edit the flags ConfigMap (`<release>-flags`). Set `enabled: false`, or list a cluster in `disabled_clusters` or a service in `disabled_services`. The orchestrator reads the file again without a restart.
