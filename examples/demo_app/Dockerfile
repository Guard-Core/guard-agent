FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

COPY pyproject.toml README.md ./
COPY guard_agent/ guard_agent/

RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python /build && \
    uv pip install --python /app/.venv/bin/python fastapi uvicorn

FROM python:3.12-slim

RUN groupadd --system guard && \
    useradd --system --gid guard --create-home guard

WORKDIR /app

COPY --from=builder --chown=guard:guard /app/.venv /app/.venv
COPY --chown=guard:guard examples/demo_app/main.py /app/main.py

LABEL org.opencontainers.image.title="guard-agent demo" \
    org.opencontainers.image.description="Containerized Guard Agent demo: a FastAPI app streaming security telemetry to a Guard Core ingestion endpoint" \
    org.opencontainers.image.source="https://github.com/Guard-Core/guard-agent" \
    org.opencontainers.image.licenses="MIT"

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GUARD_AGENT_ENDPOINT=https://api.guard-core.com \
    GUARD_AGENT_PROJECT_ID=demo-project

USER guard

EXPOSE 8000

CMD ["uvicorn", "--host", "0.0.0.0", "--port", "8000", "main:app"]
