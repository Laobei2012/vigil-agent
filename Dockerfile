# One image for the orchestrator, the receiver, and the tool gateway.
# The tool allowlist (src/vigil_agent/gateway/allowlist.yaml) is part of this image.
FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.12.11-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.11-slim-bookworm
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/Laobei2012/vigil-agent" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.description="Vigil on-call agent"
RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin vigil
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 HOME=/tmp
USER 10001
ENTRYPOINT ["vigil-agent"]
CMD ["orchestrator"]
