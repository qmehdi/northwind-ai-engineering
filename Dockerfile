# One image recipe for every Northwind service. The APP build argument picks the
# ASGI app; the ARTIFACTS argument picks which artifact directories are baked in.
#
#   docker build --build-arg APP=nw.triage.service:app   --build-arg ARTIFACTS="triage"          -t nw-triage .
#   docker build --build-arg APP=nw.semantic.service:app --build-arg ARTIFACTS="semantic index"  -t nw-semantic .
#   docker build --build-arg APP=nw.policy.service:app   --build-arg ARTIFACTS="policy" --build-arg HF_MODELS=1 -t nw-policy .
#   docker build --build-arg APP=nw.agent.service:app    --build-arg ARTIFACTS="triage semantic index policy" --build-arg HF_MODELS=1 -t nw-agent .
#   docker build --build-arg APP=mcp --build-arg ARTIFACTS="triage semantic index policy" --build-arg HF_MODELS=1 -t nw-mcp .
#   docker build --build-arg APP=pipelines --build-arg ARTIFACTS="" --build-arg EXTRAS="--extra dl --extra mlops --extra pipelines --extra platform-aws --extra platform-gcp --extra platform-azure" -t nw-pipelines .
#     (the pipelines image runs the steps under Kubeflow, Vertex or SageMaker; it carries kfp, the production summaries in
#      data/golden as a fallback for the ones the deploy copies to <artifacts>/baselines/, and the Project 2 base encoder.
#      Built by make local-up, scripts/images_gcp.sh (plus --extra platform-gcp), make images-aws and the AWS delivery pipeline)
#
# Weights and indexes are loaded once at startup, never per request. Nothing in
# the image reaches the Hub at runtime: HF_MODELS=1 pre-downloads the embedder and
# reranker at build time into /app/hf. Only the artifacts named in ARTIFACTS reach the
# final image, selected in their own stage so no layer ever holds the rest.
#
# Base images are pinned by digest, so a rebuild next month produces the same layers and
# a tag moved upstream cannot change what ships. Each digest is the multi-arch index
# (linux/amd64 and linux/arm64 resolve from it). Fetched 2026-09-28 with
# `docker buildx imagetools inspect <image>`; refresh them on purpose, with a changelog line.
#   python:3.12-slim  index sha256:f77ac9e4...  amd64 sha256:44ff437b...  arm64 sha256:950206c3...
#   uv:0.12.19        index sha256:04d046b1...  amd64 sha256:d46db4c7...  arm64 sha256:de342e01...
#   lambda-adapter    index sha256:17cfd08e...  amd64 sha256:9d6b29a4...  arm64 sha256:98d6d1dc...
ARG PYTHON_IMAGE=python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424
ARG LAMBDA_ADAPTER_IMAGE=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0@sha256:17cfd08eff1dfea3f6a9a1e9c65fdac80aa4919b6085e746615530f43f57d2f1
FROM ${UV_IMAGE} AS uv
FROM ${LAMBDA_ADAPTER_IMAGE} AS lambda-adapter
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock .python-version README.md ./
ARG EXTRAS="--extra dl --extra agents --extra agents-aws --extra agents-gcp --extra platform-azure"
RUN uv sync --frozen --no-dev --no-install-project $EXTRAS
COPY nw ./nw
RUN uv sync --frozen --no-dev $EXTRAS

# Pick the artifacts this service needs. Training checkpoints and the fp32 ONNX graph
# never ship; the int8 graph and the indexes do.
FROM ${PYTHON_IMAGE} AS artifacts
ARG ARTIFACTS="triage"
COPY artifacts/ /src/
RUN mkdir -p /out && for a in $ARTIFACTS; do cp -r "/src/$a" "/out/$a"; done \
    && rm -f /out/*/model.onnx /out/*/checkpoint.pt /out/*/best.pt /out/*/*/model.onnx /out/*/*/checkpoint.pt /out/*/*/best.pt

FROM ${PYTHON_IMAGE} AS runtime
ARG APP=nw.triage.service:app
ARG HF_MODELS=0
ARG PORT=8000
# LAMBDA=1 adds the AWS Lambda Web Adapter (1.1.0, multi-arch) as an extension so the
# same uvicorn process serves behind a Lambda function URL. Readiness waits on /readyz
# and async init lets model loading run past Lambda's 10 second init window.
ARG LAMBDA=0
COPY --from=lambda-adapter /lambda-adapter /tmp/lambda-adapter
RUN if [ "$LAMBDA" = "1" ]; then mkdir -p /opt/extensions && mv /tmp/lambda-adapter /opt/extensions/lambda-adapter; else rm -f /tmp/lambda-adapter; fi
ENV AWS_LWA_PORT=${PORT} AWS_LWA_READINESS_CHECK_PATH=/readyz AWS_LWA_ASYNC_INIT=true AWS_LWA_INVOKE_MODE=buffered
RUN useradd --create-home --uid 10001 nw && mkdir -p /app/hf /tmp/traces && chown -R nw:nw /app /tmp/traces
WORKDIR /app
COPY --from=builder --chown=nw:nw /app/.venv /app/.venv
COPY --from=builder --chown=nw:nw /app/nw /app/nw
COPY --from=artifacts --chown=nw:nw /out/ /app/artifacts/
COPY --chown=nw:nw data/accounts.json /app/data/accounts.json
# The committed production summaries (two small files): a pipeline step run with the repo
# defaults finds them where `nw.pipelines.params` says. .dockerignore lets only these through.
COPY --chown=nw:nw data/golden/triage_production.json data/golden/semantic_production.json /app/data/golden/
ENV PATH="/app/.venv/bin:$PATH" HF_HOME=/app/hf HF_HUB_OFFLINE=0 \
    NW_TRIAGE_MODEL=/app/artifacts/triage/latest NW_SEMANTIC_ARTIFACT=/app/artifacts/semantic \
    NW_INDEX=/app/artifacts/index NW_POLICY_INDEX=/app/artifacts/policy \
    NW_APP=${APP} PORT=${PORT} NW_LOG_FORMAT=json NW_TRACE_DIR=/tmp/traces
USER nw
RUN if [ "$HF_MODELS" = "1" ]; then python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')" ; fi
# The pipelines image fine-tunes Project 2 offline, so its base encoder is baked in too.
RUN if [ "$APP" = "pipelines" ]; then python -c "\
from transformers import AutoModel, AutoTokenizer; \
AutoTokenizer.from_pretrained('distilbert-base-uncased'); AutoModel.from_pretrained('distilbert-base-uncased')" ; fi
ENV HF_HUB_OFFLINE=1
EXPOSE ${PORT}
HEALTHCHECK --interval=15s --timeout=3s --start-period=60s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/readyz')" || exit 1
# The MCP image runs the server directly; every other image runs uvicorn, on AIP_HTTP_PORT when
# a Vertex endpoint set it (the Agent Platform's custom container contract) and on PORT otherwise.
CMD ["sh", "-c", "if [ \"$NW_APP\" = mcp ]; then exec python -m nw.agent.mcp_server; else exec uvicorn ${NW_APP} --host 0.0.0.0 --port ${AIP_HTTP_PORT:-$PORT} --timeout-graceful-shutdown 20; fi"]

# The serving target for a Vertex endpoint (ADR 0008): the same runtime, no artifact baked in,
# listening on AIP_HTTP_PORT and answering AIP_HEALTH_ROUTE and AIP_PREDICT_ROUTE beside the
# course routes. The platform names the artifact in AIP_STORAGE_URI (honoured like NW_MODEL_URI)
# when the registered model carries one; the registry attaches the image through
# NW_GCP_SERVING_IMAGE. The default build (no --target) is still the runtime stage above.
#   docker build --target serving --build-arg APP=nw.triage.service:app --build-arg ARTIFACTS="" -t nw-triage-serving .
#   docker build --target serving --build-arg APP=nw.semantic.service:app --build-arg ARTIFACTS="" -t nw-semantic-serving .
FROM runtime AS serving
ENV PORT=8080 AIP_HTTP_PORT=8080 AIP_HEALTH_ROUTE=/health AIP_PREDICT_ROUTE=/predict
EXPOSE 8080
