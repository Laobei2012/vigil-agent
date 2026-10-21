-- Vigil state store. Every statement is idempotent, so startup runs this file.

-- Intake events that wait for processing (the test cluster has no SQS).
CREATE TABLE IF NOT EXISTS inbox (
    id              bigserial PRIMARY KEY,
    event_id        text NOT NULL UNIQUE,
    incident_id     text NOT NULL,
    source          text NOT NULL,
    payload         jsonb NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    status          text NOT NULL DEFAULT 'pending',   -- pending, done, dead, dropped
    attempts        int NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text
);
CREATE INDEX IF NOT EXISTS inbox_pending ON inbox (incident_id, id) WHERE status = 'pending';

-- Dedupe on the PagerDuty (or Alertmanager) event ID.
CREATE TABLE IF NOT EXISTS processed_events (
    event_id     text PRIMARY KEY,
    processed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS investigations (
    id               bigserial PRIMARY KEY,
    grouping_key     text NOT NULL,
    grouping_open    boolean NOT NULL DEFAULT true,
    window_start     timestamptz NOT NULL DEFAULT now(),
    service          text NOT NULL,
    alert_name       text NOT NULL,
    status           text NOT NULL,      -- queued, running, held, done, failed, cancelled, discarded
    early_start      boolean NOT NULL DEFAULT false,
    held             boolean NOT NULL DEFAULT false,   -- early start, not paged yet
    step             text NOT NULL DEFAULT 'new',      -- new, prefetched, fast_done, deep_done
    context          jsonb NOT NULL DEFAULT '{}'::jsonb,
    prefetch         jsonb,
    fast_report      jsonb,
    deep_report      jsonb,
    gaps             jsonb NOT NULL DEFAULT '[]'::jsonb,
    useful_queries   jsonb,
    slack_channel    text,
    slack_ts         text,
    reply_ts         text,
    resolved         boolean NOT NULL DEFAULT false,
    lease_owner      text,
    lease_expires_at timestamptz,
    attempts         int NOT NULL DEFAULT 0,
    tool_calls       int NOT NULL DEFAULT 0,
    tokens_in        bigint NOT NULL DEFAULT 0,
    tokens_out       bigint NOT NULL DEFAULT 0,
    cost_usd         numeric(12, 4) NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    intake_at        timestamptz NOT NULL DEFAULT now()
);
-- One open grouping window per key: concurrent claims create exactly one investigation.
CREATE UNIQUE INDEX IF NOT EXISTS investigations_open_key
    ON investigations (grouping_key) WHERE grouping_open;
CREATE INDEX IF NOT EXISTS investigations_status ON investigations (status, early_start, created_at);
CREATE INDEX IF NOT EXISTS investigations_service ON investigations (service, alert_name, created_at DESC);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id      text PRIMARY KEY,
    investigation_id bigint REFERENCES investigations (id),
    source           text NOT NULL,
    service          text,
    title            text,
    html_url         text,
    status           text NOT NULL,      -- triggered, acknowledged, resolved
    labels           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incidents_investigation ON incidents (investigation_id);

-- Every model call and tool call with arguments and result (audit, 90 days).
CREATE TABLE IF NOT EXISTS transcripts (
    id               bigserial PRIMARY KEY,
    investigation_id bigint NOT NULL,
    pass             text NOT NULL,
    kind             text NOT NULL,      -- model_request, model_response, tool_call, note
    payload          jsonb NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS transcripts_investigation ON transcripts (investigation_id, id);
CREATE INDEX IF NOT EXISTS transcripts_created ON transcripts (created_at);

CREATE TABLE IF NOT EXISTS spend (
    day       date NOT NULL,
    kind      text NOT NULL,             -- investigation, early_start
    usd       numeric(12, 4) NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind)
);

CREATE TABLE IF NOT EXISTS feedback (
    id               bigserial PRIMARY KEY,
    investigation_id bigint,
    rating           text NOT NULL,
    slack_user       text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- Pending alerts that went back to inactive, for flap detection in early start.
CREATE TABLE IF NOT EXISTS pending_flaps (
    day          date NOT NULL,
    grouping_key text NOT NULL,
    count        int NOT NULL DEFAULT 0,
    PRIMARY KEY (day, grouping_key)
);

-- Slack messages in dry-run mode (rollout stage 1: no posting).
CREATE TABLE IF NOT EXISTS slack_outbox (
    id         bigserial PRIMARY KEY,
    action     text NOT NULL,            -- post, update
    channel    text NOT NULL,
    ts         text NOT NULL,
    thread_ts  text,
    text       text,
    blocks     jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
