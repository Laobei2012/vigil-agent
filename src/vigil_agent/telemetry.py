"""Prometheus metrics. The names follow the Observability section of the design doc."""

from prometheus_client import Counter, Gauge, Histogram

INVESTIGATION_DURATION = Histogram(
    "vigil_investigation_duration_seconds",
    "Time from intake to the end of a pass.",
    ["phase"],
    buckets=(15, 30, 60, 90, 120, 180, 240, 300, 600, 900, 1200),
)
TOOL_CALLS = Counter("vigil_tool_calls_total", "Tool calls by tool and status.", ["tool", "status"])
TOOL_LATENCY = Histogram("vigil_tool_latency_seconds", "Tool call latency.", ["tool"])
BEDROCK_TOKENS = Counter("vigil_bedrock_tokens_total", "Model tokens.", ["direction"])
BEDROCK_THROTTLES = Counter("vigil_bedrock_throttles_total", "Throttled model calls.")
COST_USD = Counter("vigil_cost_usd_total", "Estimated model cost.", ["kind"])
REPORTS = Counter("vigil_reports_total", "Reports posted by outcome.", ["outcome"])
FEEDBACK = Counter("vigil_feedback_total", "Feedback button clicks.", ["rating"])
INTAKE_EVENTS = Counter("vigil_intake_events_total", "Intake events.", ["source", "result"])
INVESTIGATIONS_RUNNING = Gauge("vigil_investigations_running", "Investigations on this pod.")
INTAKE_DEAD = Counter("vigil_intake_dead_letters_total", "Intake events moved to the dead letter.")
