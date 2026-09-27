# One image recipe for every Northwind service. The APP build argument picks the
# ASGI app; the ARTIFACTS argument picks which artifact directories are baked in.
#
#   docker build --build-arg APP=nw.triage.service:app   --build-arg ARTIFACTS="triage"          -t nw-triage .
#   docker build --build-arg APP=nw.semantic.service:app --build-arg ARTIFACTS="semantic index"  -t nw-semantic .
#   docker build --build-arg APP=nw.policy.service:app   --build-arg ARTIFACTS="policy" --build-arg HF_MODELS=1 -t nw-policy .
#   docker build --build-arg APP=nw.agent.service:app    --build-arg ARTIFACTS="triage semantic index policy" --build-arg HF_MODELS=1 -t nw-agent .
#   docker build --build-arg APP=mcp --build-arg ARTIFACTS="triage semantic index policy" --build-arg HF_MODELS=1 -t nw-mcp .
#
# Weights and indexes are loaded once at startup, never per request. Nothing in
# the image reaches the Hub at runtime: HF_MODELS=1 pre-downloads the embedder and
# reranker at build time into /app/hf.
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.6.9 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project --extra dl --extra agents --extra agents-aws --extra agents-gcp
COPY nw ./nw
RUN uv sync --frozen --no-dev --extra dl --extra agents --extra agents-aws --extra agents-gcp

FROM python:3.12-slim AS runtime
ARG APP=nw.triage.service:app
ARG ARTIFACTS="triage"
ARG HF_MODELS=0
ARG PORT=8000
# LAMBDA=1 adds the AWS Lambda Web Adapter (1.1.0, multi-arch) as an extension so the
# same uvicorn process serves behind a Lambda function URL. Readiness waits on /readyz
# and async init lets model loading run past Lambda's 10 second init window.
ARG LAMBDA=0
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 /lambda-adapter /tmp/lambda-adapter
RUN if [ "$LAMBDA" = "1" ]; then mkdir -p /opt/extensions && mv /tmp/lambda-adapter /opt/extensions/lambda-adapter; else rm -f /tmp/lambda-adapter; fi
ENV AWS_LWA_PORT=${PORT} AWS_LWA_READINESS_CHECK_PATH=/readyz AWS_LWA_ASYNC_INIT=true AWS_LWA_INVOKE_MODE=buffered
RUN useradd --create-home --uid 10001 nw
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/nw /app/nw
COPY data/accounts.json /app/data/accounts.json
COPY artifacts/ /tmp/artifacts/
RUN mkdir -p /app/artifacts && for a in $ARTIFACTS; do \
      cp -r /tmp/artifacts/$a /app/artifacts/$a; done \
    && rm -rf /tmp/artifacts /app/artifacts/*/model.onnx /app/artifacts/*/checkpoint.pt
ENV PATH="/app/.venv/bin:$PATH" HF_HOME=/app/hf HF_HUB_OFFLINE=0 \
    NW_TRIAGE_MODEL=/app/artifacts/triage/latest NW_SEMANTIC_ARTIFACT=/app/artifacts/semantic \
    NW_INDEX=/app/artifacts/index NW_POLICY_INDEX=/app/artifacts/policy \
    NW_APP=${APP} PORT=${PORT} NW_LOG_FORMAT=json NW_TRACE_DIR=/tmp/traces
RUN if [ "$HF_MODELS" = "1" ]; then python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')" ; fi
ENV HF_HUB_OFFLINE=1
RUN chown -R nw:nw /app
USER nw
EXPOSE ${PORT}
HEALTHCHECK --interval=15s --timeout=3s --start-period=60s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/readyz')" || exit 1
# The MCP image runs the server directly; every other image runs uvicorn.
CMD ["sh", "-c", "if [ \"$NW_APP\" = mcp ]; then exec python -m nw.agent.mcp_server; else exec uvicorn ${NW_APP} --host 0.0.0.0 --port ${PORT} --timeout-graceful-shutdown 20; fi"]
