# The Local track

The whole course on one laptop, no cloud account (ADR 0011): a Docker Compose stack with one
open-source service per managed box of the AWS and Google Cloud tracks, and
`nw/platform/local.py` implementing the platform contract (`nw/platform/base.py`) on it.

```bash
make setup-local          # the platform clients into the virtualenv, checks Docker Compose
make local-up             # build, push to the local registry, start platform and observability
make local-status         # every container's health, what is live, the canary weights
make local-down           # stop everything, keep the volumes
```

Docker needs 16 GB of memory (Docker Desktop: Settings, Resources) and about 40 GB of disk:
the course images, the platform images and the two Ollama models (`llama-guard3:1b`, 1.6 GB,
and `gpt-oss:20b`, 13.8 GB). The first `make local-up` builds the course images (10 to 20
minutes) and pulls the models in the background; everything else is up in about two minutes.

## What runs

Profiles select what starts. `make local-up` starts `platform` and `observability`; the
course services (no profile) always start.

| Box | Service | Image | Host port | Why it is here |
| --- | --- | --- | --- | --- |
| Course services | `triage`, `semantic`, `policy`, `resolver`, `agent` and three specialists, `mcp` | built here, pulled from the local registry | 8001, 8002, 8003, 8014, 8010, 8020 | the application layer, unchanged across tracks |
| Image registry | `registry` | `registry:3.1.2` | 5050 | the services run a pushed tag, as on ECR and Artifact Registry |
| State | `postgres` | `postgres:17.11-alpine` | (internal) | MLflow's backend store and LiteLLM's keys and spend |
| Object store | `rustfs` | `rustfs/rustfs:1.0.0` | 9000 (S3), 9001 (console) | MLflow artifacts; see the MinIO note below |
| Registry, tracking, prompts | `mlflow` | `ghcr.io/mlflow/mlflow:v3.16.1` plus boto3 and psycopg2 (`deploy/local/mlflow`) | 5001 | model registry with aliases, prompt registry, artifact proxy |
| Vectors | `qdrant` | `qdrant/qdrant:v1.19.1` | 6333 (REST), 6334 (gRPC) | the vector store behind the policy service |
| Open models | `ollama` | `ollama/ollama:0.34.4` | 11435 | `gpt-oss:20b` as Workhorse and Economy, `llama-guard3:1b` as the guardrail |
| Model gateway | `litellm` | `ghcr.io/berriai/litellm:v1.103.0` | 4000 | one door for every model call: role names, tenant keys and budgets, Llama Guard |
| Monitoring | `evidently`, `drift-reports` | `python:3.12-slim` plus evidently 0.7.23 (`deploy/local/evidently`) | 8030 | drift reports over the services' capture files, every 15 minutes |
| Serving tier | `serving-stable`, `serving-canary`, `proxy` | the course triage image plus mlflow (`deploy/local/serving`), `nginx:1.30.5-alpine` | 8005 | the registered model served from the registry, canary by replica weight |
| Higher environment | `serving-stable-higher`, `serving-canary-higher`, `proxy-higher` | same | 8105 | the promotion target (`--profile higher`) |
| Tracing | `otel-collector`, `jaeger` | `otel/opentelemetry-collector-contrib:0.161.0`, `jaegertracing/jaeger:2.21.0` | 4318, 16686 | OTLP in, traces in Jaeger |
| Metrics | `prometheus`, `grafana` | `prom/prometheus:v3.15.0`, `grafana/grafana:13.2.2` | 9090, 3000 | every `/metrics`, one provisioned dashboard (admin / `northwind`) |

One-shot jobs run once per `up` and exit: `rustfs-init` (the `mlflow` bucket), `ollama-pull`
(the two models), `litellm-keys` (the tenant keys from `deploy/local/litellm/tenants.tsv`).
Every image is pinned by tag and digest in `docker-compose.yml` (fetched 2026-09-29).

Every long-running service has a Docker health check; `make local-status` prints them. The
one exception is the OpenTelemetry collector, a distroless image with no shell for a probe:
Prometheus scrapes its own metrics on :8888, which is where its health shows.

**MinIO.** ADR 0011 names MinIO for the object store. MinIO withdrew its public container
images from Docker Hub in 2025 (the repository answers 404, the last cached tag on this
machine is `RELEASE.2025-04-22`), so a participant cannot pull it. RustFS 1.0.0 (Apache-2.0,
S3 API, multi-arch) takes the box; nothing in the course talks to the store directly, MLflow's
artifact proxy does, so the swap is one compose service.

## How each box maps to the clouds

| Contract | Local | AWS | Google Cloud |
| --- | --- | --- | --- |
| `ModelRegistry` | MLflow registry: `<environment>-<tenant>-<name>`, aliases `candidate`, `approved`, `live`, `retired` | SageMaker Model Registry: model package groups, approval status | Vertex Model Registry with aliases |
| `PipelineRunner` | Kubeflow Pipelines SDK, local runner (`kfp.local`, subprocess or Docker) | SageMaker Pipelines | Vertex AI Pipelines (the same Kubeflow definition) |
| `EndpointClient` | MLflow model serving, two replicas behind nginx with weights | real-time endpoint with a CodeDeploy canary; Serverless Inference per tenant | Vertex endpoint with a traffic split; Cloud Run per tenant |
| `PromptStore` | MLflow prompt registry, tag `sha256_12` | Bedrock Prompt Management | Gen AI SDK prompt management |
| `VectorStore` | Qdrant, one collection per tenant and corpus | Bedrock Knowledge Bases on S3 Vectors | RAG Engine on Vector Search |
| `AgentRuntime` | the `resolver` container (`/invocations`, `/api/reasoning_engine`) | AgentCore Runtime | Agent Engine |
| Model gateway | LiteLLM with a key and budget per tenant | LiteLLM on ECS Fargate (the multi-provider gateway guidance) | LiteLLM on Cloud Run |
| Guardrail | Llama Guard 3 through Ollama, a LiteLLM pre-call guardrail | Bedrock Guardrails | Model Armor |
| Monitoring | Evidently reports over the capture files | Model Monitor | Vertex Model Monitoring |
| Object store | RustFS (S3 API) | S3 | Cloud Storage |
| Image registry | `registry:3` on :5050 | ECR | Artifact Registry |
| Tracing and metrics | OpenTelemetry collector, Jaeger, Prometheus, Grafana | X-Ray, CloudWatch | Cloud Trace, Cloud Monitoring |
| Delivery | `make local-up`, `make local-canary`, `make local-promote`, the `higher` profile | CodePipeline, CodeBuild, CodeDeploy | Cloud Build, Cloud Deploy |

## The model gateway and the models

Every container calls models through LiteLLM on `http://litellm:4000` with the tenant's key
(`NW_GATEWAY_URL`, `NW_GATEWAY_KEY` in the compose environment). Model names are the course's
role ids, so `nw/config.py` needs no change:

| Gateway name | Without a cloud key | With `ANTHROPIC_API_KEY` |
| --- | --- | --- |
| `gpt-oss:20b`, `workhorse`, `economy` | `ollama_chat/gpt-oss:20b` | same |
| `gpt-oss:120b` | `ollama_chat/gpt-oss:120b` (gpu profile) | same |
| `judge` | `ollama_chat/gpt-oss:20b` (a local stand-in; the course client stays judge-free) | `anthropic/claude-opus-5` |
| `claude-opus-5` | not served (404: the client falls back) | `anthropic/claude-opus-5` |
| `guard` | `ollama_chat/llama-guard3:1b` | same |

The container starts from `deploy/local/litellm/config.yaml`, or `config-cloud-judge.yaml`
when `ANTHROPIC_API_KEY` is set. Tenant keys and monthly budgets come from
`deploy/local/litellm/tenants.tsv`; `litellm-keys` applies the file on every start, so edit a
budget there and `docker compose --profile platform up -d litellm-keys`. Spend per tenant:
`curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" localhost:4000/spend/logs`.

Llama Guard screens the last user turn of every completion (`deploy/local/litellm/llama_guard.py`);
an `unsafe` verdict is a 400 that names the category, a guard that cannot answer is a 503
(`NW_GUARD_FAIL_OPEN=1` lets calls through instead, with a warning in the gateway log).

**Ollama on a Mac.** Containers have no GPU on macOS, so `gpt-oss:20b` inside the stack runs on
CPU and holds about 14 GB, which does not fit beside the services in a 16 GB Docker VM. Run
Ollama natively (Metal, host memory) and point the gateway at it:

```bash
# .env
NW_COMPOSE_OLLAMA_INTERNAL_URL=http://host.docker.internal:11434
NW_OLLAMA_MODELS_DIR=/Users/<you>/.ollama      # share the model store; the pull job then finds it
```

On Linux with a GPU, `make local-up GPU=1` adds `docker-compose.gpu.yml` (the NVIDIA device
reservation) and the `gpu` profile, which pulls `gpt-oss:120b` and moves the `workhorse` name
to it.

## The serving tier and the canary

`serving-stable` serves `models:/<environment>-<tenant>-triage@live`, `serving-canary` serves
the alias `canary`, and nginx on :8005 splits traffic by the weights in
`deploy/local/proxy/upstream.conf`. The weights are the promotion control:

```bash
make local-canary WEIGHT=10      # 90 percent stable, 10 percent canary; a reload, not a restart
curl -s localhost:8005/weights   # {"stable": 90, "canary": 10}
make local-canary WEIGHT=0       # back to stable only
```

nginx rejects `weight=0`, so a replica at zero is written as `weight=1 down`, which nginx never
routes to; `nw.platform.local.parse_upstream` reads it back as zero. Each response carries
`X-Upstream` with the replica that answered.

`LocalEndpointClient.deploy(version, canary_percent=10)` moves the alias `canary`, restarts
the canary replica and sets the weights; `deploy(version, live=True)` moves `live`, restarts
the stable replica and sends everything back to it. The serving replicas wait for their alias
to exist, so a fresh stack shows them unhealthy until the first model is registered:
`make local-up` registers `artifacts/triage/latest` when it exists (`make local-bootstrap`
does it again).

## Promotion: the `higher` profile

The cloud tracks build one environment and describe how the same pipeline promotes into a
higher one through a deployment role and a second target (ADR 0008). Locally the second
target is a second serving tier under the `higher` profile, reading the same registry under
the environment name `higher`:

```bash
make local-higher-up             # serving-stable-higher, serving-canary-higher, proxy on :8105
make local-promote               # copy models:/northwind-<tenant>-triage@live into
                                 # higher-<tenant>-triage, alias live, restart its stable replica
curl -s localhost:8105/weights
```

`local-promote` copies the version (`MlflowClient.copy_model_version`), never retrains: the
artifact that passed the gate in the lower environment is byte for byte the one that goes
live in the higher one, and the copy carries `promoted_from` with the source URI. The higher
tier has its own weights file (`deploy/local/proxy/higher/upstream.conf`) and can run its own
canary with the same client (`stable="serving-stable-higher"`).

## Measured memory

Not measured yet. The first full start on the author's machine ran out of disk in the Docker VM
(71 GB, 62 GB held by unrelated images) while building the course images, so only the local
registry ran: `registry` 37.75 MiB (`docker stats`, 2026-09-29). Fill this table from
`docker stats --no-stream` after the first `make local-up` that reaches health; estimates
are not wanted here.

| Service | RAM (docker stats) |
| --- | --- |
| registry | 37.75 MiB |
| every other service | to measure |

## Environment variables

Everything has a default in `docker-compose.yml`; the ones worth setting in `.env`:

```bash
NW_TENANT=solo                                   # cohort mode: your handle; names every registry entry
NW_ENVIRONMENT=northwind
NW_GATEWAY_URL=http://localhost:4000             # the platform from the laptop (containers use litellm:4000)
NW_GATEWAY_KEY=sk-nw-solo-change-me              # your line in deploy/local/litellm/tenants.tsv
NW_MLFLOW_URI=http://localhost:5001              # make train-triage registers here instead of the sqlite file
NW_QDRANT_URL=http://localhost:6333
NW_ENDPOINT_URL=http://localhost:8005
NW_AGENT_RUNTIME_URL=http://localhost:8014
NW_PIPELINE_ROOT=artifacts/pipelines             # kfp local runs, one directory per tenant and run
NW_KFP_RUNNER=subprocess                         # or docker
NW_REGISTRY=localhost:5050
NW_IMAGE_TAG=dev                                 # the tag make local-up builds and pushes
LITELLM_MASTER_KEY=sk-nw-master-change-me
ANTHROPIC_API_KEY=                               # set it and the Judge role reaches Claude through the gateway
NW_COMPOSE_OLLAMA_INTERNAL_URL=http://ollama:11434   # http://host.docker.internal:11434 for a native Ollama
NW_OLLAMA_MODELS_DIR=ollama-models               # a named volume, or a path such as ~/.ollama
NW_OLLAMA_PULL="llama-guard3:1b gpt-oss:20b"
NW_S3_ACCESS_KEY=nw
NW_S3_SECRET_KEY=northwind-secret
NW_DB_PASSWORD=northwind
NW_GRAFANA_PASSWORD=northwind
```

## Files

```
docker-compose.yml, docker-compose.gpu.yml
deploy/local/local.sh                  the commands behind make local-*
deploy/local/mlflow/                   the tracking server image (boto3, psycopg2) and the bucket init
deploy/local/serving/                  the serving image (course image plus mlflow) and its wait-then-serve script
deploy/local/proxy/                    nginx.conf, upstream.conf and weights.json (written by the client), higher/
deploy/local/litellm/                  config.yaml, config-cloud-judge.yaml, llama_guard.py, tenants.tsv, keys.sh
deploy/local/ollama/pull.sh            the model pull job
deploy/local/evidently/                the report image, report.py, run.sh
deploy/local/prometheus/prometheus.yml
deploy/local/grafana/                  provisioning and dashboards/northwind.json
deploy/local/postgres/init.sql
deploy/local/registry/                 agent cards (<environment>-<tenant>.json) and env files written by the runtime
nw/platform/local.py                   the platform implementation and the CLI
nw/platform/local_pyfunc.py            the pyfunc the registry logs and the serving tier loads
tests/platform/test_local_*.py         offline tests; test_local_live.py needs the stack
```
