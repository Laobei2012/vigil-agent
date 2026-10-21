"""Runtime configuration, read from environment variables with the prefix VIGIL_."""

from __future__ import annotations

import os
import socket
import uuid
from functools import cached_property

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_owner() -> str:
    return f"{os.environ.get('POD_NAME') or socket.gethostname()}-{uuid.uuid4().hex[:6]}"


class Settings(BaseSettings):
    """Orchestrator and receiver configuration."""

    model_config = SettingsConfigDict(env_prefix="VIGIL_", extra="ignore")

    cluster: str = "oci-phx"
    # Incidents without a cluster label go to the primary cluster only.
    primary_cluster: bool = True
    database_url: str = "postgresql://vigil:vigil@localhost:55432/vigil"

    catalog_path: str = "catalog/services.yaml"
    flags_path: str | None = None
    prompts_dir: str | None = None

    # Intake. "direct" writes events to the PostgreSQL inbox; "sqs" sends them to SQS.
    queue: str = "direct"
    sqs_queue_url: str | None = None
    aws_region: str = "us-west-2"
    # Comma-separated, because a webhook subscription can rotate its secret.
    pd_webhook_secrets: str = ""
    pd_api_key: str | None = None
    pd_api_url: str = "https://api.pagerduty.com"
    am_webhook_token: str | None = None
    intake_max_attempts: int = 5

    # Slack. Without a bot token, or with dry_run, messages go to the slack_outbox table.
    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    slack_dry_run: bool = False
    slack_fallback_channel: str = "#vigil-test"
    slack_health_channel: str = "#vigil-test"
    link_allowlist: str = "oci.liwenxin.com,github.com,pagerduty.com,slack.com"
    grafana_url: str = "https://monitoring.oci.liwenxin.com"

    # Model. Without model_enabled the agent posts the evidence-only reply (degraded mode).
    model_enabled: bool = False
    # "bedrock" (production design) or "anthropic" (Anthropic API, ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL).
    model_provider: str = "bedrock"
    bedrock_region: str = "us-west-2"
    # Pinned on purpose: upgrades are a config change that passes the eval gate first.
    model_id: str = "us.anthropic.claude-opus-5-5"
    model_effort: str = "high"
    model_max_tokens: int = 16000
    price_input_per_mtok: float = 4.0
    price_output_per_mtok: float = 20.0

    gateway_url: str = "http://localhost:8081"
    gateway_token: str | None = None

    # Concurrency and leases.
    max_running_cluster: int = 5
    max_running_pod: int = 3
    lease_seconds: int = 90
    lease_renew_seconds: int = 30
    scheduler_interval_seconds: float = 2.0
    grouping_window_seconds: int = 300

    # Budgets.
    prefetch_timeout_seconds: float = 30.0
    fast_deadline_seconds: float = 180.0
    fast_model_seconds: float = 60.0
    fast_max_tool_calls: int = 3
    deep_max_tool_calls: int = 25
    deep_max_seconds: float = 900.0
    tool_call_timeout_seconds: float = 30.0
    investigation_token_budget: int = 600_000
    daily_spend_cap_usd: float = 50.0

    # Early start from pending alerts (vmalert or Prometheus GET /api/v1/alerts).
    early_start_enabled: bool = False
    pending_alerts_url: str | None = None
    early_start_poll_seconds: float = 15.0
    early_start_hold_seconds: int = 900
    early_start_flap_limit: int = 3

    transcript_retention_days: int = 90
    http_port: int = 8080
    owner: str = Field(default_factory=_default_owner)

    @cached_property
    def pd_secrets(self) -> list[str]:
        return [s.strip() for s in self.pd_webhook_secrets.split(",") if s.strip()]

    @cached_property
    def allowed_link_domains(self) -> list[str]:
        return [d.strip().lower() for d in self.link_allowlist.split(",") if d.strip()]
