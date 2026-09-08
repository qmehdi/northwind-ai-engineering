# Multi-stage image for any nw service. Build with a target module, for example:
#   docker build --build-arg APP=nw.triage.service:app --build-arg MODEL=artifacts/triage/latest -t nw-triage .
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.6.9 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY nw ./nw
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
ARG APP=nw.triage.service:app
ARG MODEL=artifacts/triage/latest
ARG PORT=8000
RUN useradd --create-home --uid 10001 nw
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/nw /app/nw
# Model weights are baked at build time and loaded once at startup, not per request.
COPY ${MODEL}/ /app/model/
ENV PATH="/app/.venv/bin:$PATH" NW_TRIAGE_MODEL=/app/model NW_APP=${APP} PORT=${PORT} NW_LOG_FORMAT=json
USER nw
EXPOSE ${PORT}
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/readyz')" || exit 1
# exec form through sh so the env vars expand; uvicorn handles SIGTERM for graceful shutdown.
CMD ["sh", "-c", "exec uvicorn ${NW_APP} --host 0.0.0.0 --port ${PORT} --timeout-graceful-shutdown 20"]
