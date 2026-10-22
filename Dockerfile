ARG PYTHON_VERSION=3.10
FROM python:${PYTHON_VERSION}-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /uvx /bin/

RUN useradd --create-home --uid 10001 app \
    && mkdir /app \
    && chown app:app /app

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=10 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001

COPY --chown=app:app pyproject.toml uv.lock* README.md /app/

RUN uv sync --extra dev

ENV PATH="/app/.venv/bin:$PATH"

COPY --chown=app:app guard_agent/ /app/guard_agent/
COPY --chown=app:app tests/ /app/tests/
COPY --chown=app:app examples/ /app/examples/
COPY --chown=app:app docs/ /app/docs/

RUN mkdir -p /app/data/ipinfo

# One-shot test image: there is no service to probe, so the check confirms the package imports.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD ["python", "-c", "import guard_agent"]
