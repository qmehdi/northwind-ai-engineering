# The Local track

The whole course on one laptop, no cloud account (ADR 0011): a Docker Compose stack with one
open-source service per managed box of the AWS, Google Cloud and Azure tracks, and
`nw/platform/local.py` implementing the platform contract (`nw/platform/base.py`) on it.

```bash
make local-up             # generate the secrets, build, push to the local registry, start platform and observability
make local-status         # every container's health, what is live, the canary weights
make local-bootstrap      # register artifacts/triage/latest and make it live (local-up does it once when it exists)
make local-down           # stop everything, keep the volumes
```

`make setup` installs the Local clients with everything else. `make setup-local` adds the Local
set (MLflow, the Kubeflow local runner, Qdrant) with `uv sync --inexact`, so it keeps whatever
other extras the virtualenv already holds and is safe to run in any checkout.

## What the machine needs

| Machine | Works | How |
| --- | --- | --- |
| Mac with 32 GB or more | the full stack with `gpt-oss:20b` | Docker Desktop at 16 GB (Settings, Resources) and Ollama running natively beside it (Metal, host memory, about 14 GB for `gpt-oss:20b`); see "Ollama on a Mac" below |
| Mac with 16 GB | the smaller-model profile | `make local-up SMALL=1`, native Ollama: `qwen3:4b` (2.5 GB) takes the Workhorse and Economy roles; add `OBSERVABILITY=0` if Docker runs short of memory |
| Linux with 32 GB, no GPU | the full stack, slowly (CPU inference) | Docker gets the host's memory; `gpt-oss:20b` inside the stack |
| Linux with an 80 GB GPU | the cloud tracks' Workhorse | `make local-up GPU=1` (`gpt-oss:120b`) |

16 GB is not enough for the full stack on a Mac: Docker's 16 GB and a native `gpt-oss:20b`
(13.8 GB of weights) do not fit together in 16 GB of host memory. The fallback is a profile, not
a different course: `SMALL=1` adds `docker-compose.small.yml`, which pulls `qwen3:4b` instead of
`gpt-oss:20b`, points the gateway's `workhorse` name at it and sets `NW_MODEL_WORKHORSE` and
`NW_MODEL_ECONOMY` in the course containers. Put the same two lines in your `.env` so the
laptop's clients ask for it too:

```bash
NW_MODEL_WORKHORSE=qwen3:4b
NW_MODEL_ECONOMY=qwen3:4b
```

A 4B model follows tool calls less reliably than `gpt-oss:20b`: expect more agent steps that
fail validation, and read the course's agent numbers as measured on `gpt-oss:20b`, not on it.
The stack itself was brought to full health on `SMALL=1` (see "Measured memory"); the course's
agent numbers on `qwen3:4b` are not measured. `qwen3:4b` reasons before it answers, so a call
with a small `max_tokens` can come back with an empty answer: raise the limit.

Disk: give Docker about 60 GB. Measured on 2026-09-30 after a full build with native Ollama
(`docker system df`): 21.1 GB of images (the serving image 4.2 GB, the pipelines image 2.5 GB,
each course service about 2 GB, sharing one virtualenv layer since the Dockerfile puts the shared
layers before any per-image argument), 18.1 GB of build cache and 8.9 GB of volumes, 8.3 GB of
them the local image registry. Models kept inside Docker instead of in a native Ollama add
`llama-guard3:1b` (1.6 GB) and `gpt-oss:20b` (13.8 GB), about 15 GB. A 70 GB Docker disk filled
during a rebuild before the layers were shared; `docker builder prune` after a rebuild hands the
cache back, and with a native Ollama the `northwind_ollama-models` volume stays empty and can be
removed (`docker volume rm northwind_ollama-models` with the stack down). The first
`make local-up` builds the course images and pulls the models in the background; later starts
reuse the build cache.

## Security: no authentication, by design

The Local stack is a single-user laptop platform and has no authentication of its own: MLflow,
Qdrant, Ollama, Evidently, Prometheus, Jaeger, the registry and the serving proxy answer anyone
who can reach them, and the course services check `x-api-key` only when `NW_API_KEY` is set.
Two things keep that safe on a laptop, and nothing else does:

- **Every published port is bound to 127.0.0.1**, so only this machine reaches the stack; nothing
  listens on the Wi-Fi or office network. Do not change the bindings to share the stack; use a
  cloud track for anything more than one person.
- **No default credentials.** The first `make local-up` runs `deploy/local/secrets.sh`, which
  generates the gateway master key, the Postgres and RustFS credentials, the Grafana admin
  password and one gateway key per tenant, and writes them to `.env` and
  `deploy/local/litellm/tenant-keys.tsv` (both untracked). Nothing in the repository holds a
  usable secret. The containers that hold a secret refuse to start without it (LiteLLM with no
  master key, Postgres with no password). A stack whose volumes were initialised before this
  change keeps its old credentials (Postgres, RustFS and Grafana store them at first start); the
  script records those in `.env` and says how to start over with generated ones.

What the stack does not have, stated so no one mistakes it for a pattern: TLS, users, roles,
audit logs, or tenant isolation between cohort handles (the per-tenant gateway keys and budgets
are the only per-tenant control). The cloud tracks carry those.

## What runs

Every host port below is `127.0.0.1:<port>`. Profiles select what starts. `make local-up` starts `platform` and `observability`; the
course services (no profile) always start.

| Box | Service | Image | Host port | Why it is here |
| --- | --- | --- | --- | --- |
| Course services | `triage`, `semantic`, `policy`, `resolver`, `agent` and three specialists, `mcp` | built here, pulled from the local registry | 8001, 8002, 8003, 8014, 8010, 8020 | the application layer, unchanged across tracks |
| Image registry | `registry` | `registry:3.1.2` | 5050 | the services run a pushed tag, as on ECR and Artifact Registry |
| State | `postgres` | `postgres:17.11-alpine` | (internal) | MLflow's backend store and LiteLLM's keys and spend |
| Object store | `rustfs` | `rustfs/rustfs:1.0.0` | 9000 (S3), 9001 (console) | MLflow artifacts; see the MinIO note below |
| Registry, tracking, prompts | `mlflow` | `ghcr.io/mlflow/mlflow:v3.16.1` plus boto3 and psycopg2 (`deploy/local/mlflow`) | 5001 | model registry with aliases, prompt registry, artifact proxy (5000 is AirPlay on a Mac; `make mlflow-ui` uses 5002 for a file-based store) |
| Vectors | `qdrant` | `qdrant/qdrant:v1.19.1` | 6333 (REST), 6334 (gRPC) | the vector store behind the policy service |
| Open models | `ollama` | `ollama/ollama:0.34.4` | 11435 | `gpt-oss:20b` as Workhorse and Economy, `llama-guard3:1b` as the guardrail (idle when the gateway points at a native Ollama) |
| Model gateway | `litellm` | `ghcr.io/berriai/litellm:v1.103.0` | 4000 | one door for every model call: role names, tenant keys and budgets, Llama Guard; the only service with a key |
| Monitoring | `evidently`, `drift-reports` | `python:3.12-slim` plus evidently 0.7.23 (`deploy/local/evidently`) | 8030 | drift reports over the services' capture files, every 15 minutes |
| Serving tier | `serving-stable`, `serving-canary`, `proxy` | the course triage image plus mlflow (`deploy/local/serving`), `nginx:1.30.5-alpine` | 8005 | the registered model served from the registry, canary by replica weight |
| Higher environment | `serving-stable-higher`, `serving-canary-higher`, `proxy-higher` | same | 8105 | the promotion target (`--profile higher`) |
| Tracing | `otel-collector`, `jaeger` | `otel/opentelemetry-collector-contrib:0.161.0`, `jaegertracing/jaeger:2.21.0` | 4318, 16686 | OTLP in, traces in Jaeger |
| Metrics | `prometheus`, `grafana` | `prom/prometheus:v3.15.0`, `grafana/grafana:13.2.2` | 9090, 3000 | every `/metrics`, one provisioned dashboard (admin, the password is `NW_GRAFANA_PASSWORD` in `.env`) |

One-shot jobs run once per `up` and exit: `rustfs-init` (the `mlflow` bucket), `ollama-pull`
(the models, into whichever Ollama the gateway uses: see "Ollama on a Mac"), `litellm-keys`
(the tenant budgets from `deploy/local/litellm/tenants.tsv` and their keys from the generated
`deploy/local/litellm/tenant-keys.tsv`).

Two MLflow settings exist because the replicas and jobs reach MLflow over the compose network:
MLflow 3 checks the `Host` header against DNS rebinding, so the server lists `mlflow:5000`
beside `localhost` in `--allowed-hosts`; and it advertises presigned object store URLs for
downloads, which would name `rustfs:9000`, a host the laptop cannot resolve, so the Local
client forces downloads through the MLflow server's artifact proxy (`nw/platform/local.py`).
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

| Contract | Local | AWS | Google Cloud | Azure |
| --- | --- | --- | --- | --- |
| `ModelRegistry` | MLflow registry: `<environment>-<tenant>-<name>`, aliases `candidate`, `approved`, `live`, `retired` | SageMaker Model Registry: model package groups, approval status | Model Registry on the Agent Platform (formerly Vertex AI) with aliases | Azure Machine Learning model registry, the stage as a `stage` tag |
| `PipelineRunner` | Kubeflow Pipelines SDK, local runner (`kfp.local`, subprocess or Docker) | SageMaker Pipelines | Pipelines on the Agent Platform (the same Kubeflow definition) | Azure Machine Learning pipelines on serverless compute |
| `EndpointClient` | MLflow model serving, two replicas behind nginx with weights | real-time endpoint with a CodeDeploy canary; Serverless Inference per tenant | an endpoint with a traffic split; Cloud Run per tenant | managed online endpoints, blue and green with a traffic split |
| `PromptStore` | MLflow prompt registry, tag `sha256_12` | Bedrock Prompt Management | Gen AI SDK prompt management | blobs plus data assets keyed by the prompt hash |
| `VectorStore` | Qdrant, one collection per tenant and corpus | Bedrock Knowledge Bases on S3 Vectors | RAG Engine | Azure AI Search, hybrid |
| `AgentRuntime` | the `resolver` container (`/invocations`, `/api/reasoning_engine`) | AgentCore Runtime | Agent Runtime (formerly Agent Engine) | Foundry hosted agents; the live agent on Container Apps |
| Model gateway | LiteLLM with a key and budget per tenant | LiteLLM on ECS Fargate behind CloudFront (the multi-provider gateway guidance) | LiteLLM on Cloud Run | API Management's AI gateway |
| Guardrail | Llama Guard 3 through Ollama, a LiteLLM pre-call guardrail | Bedrock Guardrails | Model Armor | Foundry content filter with Prompt Shields |
| Monitoring | Evidently reports over the capture files | Model Monitor | Model Monitoring | Azure Monitor alerts |
| Object store | RustFS (S3 API) | S3 | Cloud Storage | Blob Storage |
| Image registry | `registry:3` on :5050 | ECR | Artifact Registry | Azure Container Registry |
| Tracing and metrics | OpenTelemetry collector, Jaeger, Prometheus, Grafana | X-Ray, CloudWatch | Cloud Trace, Cloud Monitoring | Application Insights, Log Analytics |
| Delivery | `make local-up`, `make local-canary`, `make local-promote`, the `higher` profile | CodePipeline, CodeBuild, CodeDeploy | Cloud Build, Cloud Deploy | Azure Pipelines or GitHub Actions, Container Apps revisions |

## The model gateway and the models

Every container calls models through LiteLLM on `http://litellm:4000` with the tenant's key
(`NW_GATEWAY_URL`, `NW_GATEWAY_KEY` in the compose environment). Model names are the course's
role ids, so `nw/config.py` needs no change:

| Gateway name | Without a cloud key | With `ANTHROPIC_API_KEY` |
| --- | --- | --- |
| `gpt-oss:20b`, `workhorse`, `economy` | `ollama_chat/gpt-oss:20b` (`workhorse` and `economy` are `qwen3:4b` with `SMALL=1`) | same |
| `qwen3:4b` | `ollama_chat/qwen3:4b` (the `SMALL=1` model) | same |
| `gpt-oss:120b` | `ollama_chat/gpt-oss:120b` (gpu profile) | same |
| `judge` | `ollama_chat/gpt-oss:20b`, a local stand-in (`qwen3:4b` with `SMALL=1`, through `NW_GATEWAY_LOCAL_MODEL`) | `anthropic/claude-opus-5` |
| `claude-opus-5` | not served: HTTP 400 `Invalid model name` | `anthropic/claude-opus-5` |
| `guard` | `ollama_chat/llama-guard3:1b` | same |

**Which Judge the course client asks for.** With `NW_GATEWAY_URL` set (the course's setup, and
every container's), `nw.config` resolves the Judge role on the Local track to `claude-opus-5`
only when the gateway was started with `ANTHROPIC_API_KEY`, which compose and the client both read
from `.env`; otherwise the role resolves to `fake-judge`. Two settings give you a Judge:

- `ANTHROPIC_API_KEY=<key>` in `.env`, then `make local-up` again (with `SMALL=1` if you use it):
  the gateway starts from `config-cloud-judge.yaml` and the Judge is the calibrated Claude Opus 5,
  billed on your Anthropic key.
- `NW_MODEL_JUDGE=judge` in `.env` without a key: the gateway's `judge` name, `gpt-oss:20b` on
  Ollama, or `qwen3:4b` under `SMALL=1` (the alias follows `NW_GATEWAY_LOCAL_MODEL`), a weaker and
  uncalibrated stand-in.

With neither, the Judge is `fake-judge`, which the gateway does not serve: preflight's Judge round
trip fails, and a judged run (`make eval-policy`, `make agent-eval`, `make agent-gate`,
`make agent-gate-live`, `make eval-policy-baseline JUDGE=1`) stops before any model call with
`judged run refused: no Judge route on this track: ...` and exit code 2, rather than score real
answers with canned verdicts. Run them without the Judge: `--no-judge`, or `NO_JUDGE=1` on the
three agent targets. `NW_ANTHROPIC_API_KEY` is a different switch: a process without
`NW_GATEWAY_URL` calls the Anthropic API with it directly. For an EU account the Judge resolves to
`fake-judge` on this track even with a key, because the Anthropic API processes in the US or
globally; the gateway does not serve that name, so an EU judged call fails rather than leave the
zone.

The container starts from `deploy/local/litellm/config.yaml`, or `config-cloud-judge.yaml`
when `ANTHROPIC_API_KEY` is set. Tenant names and monthly budgets come from
`deploy/local/litellm/tenants.tsv`, their keys from `deploy/local/litellm/tenant-keys.tsv`
(generated, untracked; a third column in `tenants.tsv` sets a key yourself); `litellm-keys`
applies both on every start, so add a tenant or edit a budget, run `deploy/local/secrets.sh`
(it adds keys for new names) and `docker compose --profile platform up -d litellm-keys`. Spend per tenant:
`curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" localhost:4000/spend/logs`.

Llama Guard screens the last user turn of every completion (`deploy/local/litellm/llama_guard.py`);
an `unsafe` verdict is a 400 that names the category, a guard that cannot answer is a 503
(`NW_GUARD_FAIL_OPEN=1` lets calls through instead, with a warning in the gateway log).

**Ollama on a Mac.** Containers have no GPU on macOS, so `gpt-oss:20b` inside the stack runs on
CPU and holds about 14 GB, which does not fit beside the services in a 16 GB Docker VM. Run
Ollama natively (Metal, host memory) and point the gateway at it; this is the recommended setup
on every Mac:

```bash
# .env
NW_COMPOSE_OLLAMA_INTERNAL_URL=http://host.docker.internal:11434
```

The pull job pulls into the Ollama the gateway uses (`OLLAMA_HOST` follows
`NW_COMPOSE_OLLAMA_INTERNAL_URL`), so with a native Ollama the models land on the host, once, and
the in-stack `ollama` container stays idle with an empty volume. What it pulls:
`NW_OLLAMA_PULL` unset pulls `llama-guard3:1b` and the Workhorse (`gpt-oss:20b`, or `qwen3:4b`
with `SMALL=1`); `NW_OLLAMA_PULL="llama-guard3:1b"` (the cloud tracks' setting) pulls only the
guardrail; `NW_OLLAMA_PULL=` set and empty pulls nothing, for when you manage the models
yourself with `ollama pull`. A model already in the store returns at once.

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
to exist; the canary replica, with no canary version yet, serves the live one at weight 0 and
says so in its log. A fresh stack shows both unhealthy until the first model is registered:
`make local-up` registers `artifacts/triage/latest` when it exists, and `make local-bootstrap`
does it again whenever you need it (after a `make train-triage` in a fresh checkout, or to
recover a lost registry). The cloud tracks' equivalent is `make bootstrap-aws`,
`make bootstrap-gcp` or `make bootstrap-azure`.

**A fresh fork.** `artifacts/` is made by training and never committed, so a new checkout has
none. The images still build: the Dockerfile reads `artifacts/` through a bind mount and skips
what is missing, printing `no artifacts/<name> in the build context: the image ships without
it`. `make local-up` then waits only for the platform containers, not the course services, and
ends with `no artifacts/triage/latest yet: the course services report not ready until make
train-triage, then make local-bootstrap`. That is the expected state until Project 1: the
course service containers (triage, semantic, policy, the agents) answer `/healthz` but report
not ready, because the models they serve do not exist yet. After Project 1 trains a model,
`make local-bootstrap` registers it and makes it live in the serving tier, and the next
`make local-up` rebuilds the service images with the artifacts that now exist.

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

Measured with `docker stats --no-stream` on 2026-09-30, on a 32 GB Mac with Docker Desktop at
16 GB, the stack started with `SMALL=1` and native Ollama, about two hours after the first
healthy start and idle (25 containers: 24 healthy and the collector, which has no probe;
`make local-test`: 235 offline and 13 of 13 live tests passed). The containers sum to about
5.9 GiB; the native Ollama held `qwen3:4b` in about 3.4 GB of host memory on top. The Workhorse
on `gpt-oss:20b` holds about 14 GB instead, which is why a Mac needs 32 GB for it.

| Service | RAM | Service | RAM |
| --- | --- | --- | --- |
| mlflow | 1.21 GiB | agent | 76 MiB |
| litellm | 626 MiB | agent-policy | 76 MiB |
| semantic | 588 MiB | agent-triage | 75 MiB |
| serving-stable | 490 MiB | agent-resolution | 75 MiB |
| policy | 481 MiB | ollama (idle, native Ollama in use) | 76 MiB |
| serving-canary | 481 MiB | resolver | 78 MiB |
| grafana | 367 MiB | registry | 77 MiB |
| qdrant | 306 MiB | prometheus | 81 MiB |
| evidently | 226 MiB | mcp | 55 MiB |
| rustfs | 188 MiB | postgres | 48 MiB |
| triage | 169 MiB | jaeger | 46 MiB |
| otel-collector | 125 MiB | drift-reports | 27 MiB |
| proxy | 3 MiB | | |

## Environment variables

Everything that is not a secret has a default in `docker-compose.yml`. The secrets have none:
`deploy/local/secrets.sh` (run by `make local-up`) writes `LITELLM_MASTER_KEY`, `NW_DB_PASSWORD`,
`NW_S3_ACCESS_KEY`, `NW_S3_SECRET_KEY`, `NW_GRAFANA_PASSWORD` and your tenant's `NW_GATEWAY_KEY`
into `.env`. The others worth setting there:

```bash
NW_TENANT=solo                                   # cohort mode: your handle; names every registry entry
NW_ENVIRONMENT=northwind
NW_GATEWAY_URL=http://localhost:4000             # the platform from the laptop (containers use litellm:4000)
# NW_GATEWAY_KEY                                 generated; another tenant's is in deploy/local/litellm/tenant-keys.tsv
NW_MLFLOW_URI=http://localhost:5001              # make train-triage registers here instead of the sqlite file
NW_QDRANT_URL=http://localhost:6333
NW_ENDPOINT_URL=http://localhost:8005
NW_AGENT_RUNTIME_URL=http://localhost:8014
NW_PIPELINE_ROOT=artifacts/pipelines             # kfp local runs, one directory per tenant and run
NW_KFP_RUNNER=subprocess                         # or docker
NW_REGISTRY=localhost:5050
NW_IMAGE_TAG=dev                                 # the tag make local-up builds and pushes
ANTHROPIC_API_KEY=                               # set it and the gateway serves claude-opus-5 for the Judge role
NW_MODEL_JUDGE=                                  # judge for the local stand-in without a key (qwen3:4b under SMALL=1)
NW_COMPOSE_OLLAMA_INTERNAL_URL=http://ollama:11434   # http://host.docker.internal:11434 for a native Ollama (every Mac)
NW_OLLAMA_MODELS_DIR=ollama-models               # the in-stack Ollama's store: a named volume, or a path such as ~/.ollama
NW_OLLAMA_PULL="llama-guard3:1b gpt-oss:20b"     # unset: these (qwen3:4b with SMALL=1); empty: none
```

## Files

```
docker-compose.yml, docker-compose.gpu.yml, docker-compose.small.yml
deploy/local/local.sh                  the commands behind make local-*
deploy/local/secrets.sh                generates the stack's secrets into .env on the first local-up
deploy/local/mlflow/                   the tracking server image (boto3, psycopg2) and the bucket init
deploy/local/serving/                  the serving image (course image plus mlflow) and its wait-then-serve script
deploy/local/proxy/                    nginx.conf, upstream.conf and weights.json (written by the client), higher/
deploy/local/litellm/                  config.yaml, config-cloud-judge.yaml, llama_guard.py, tenants.tsv, keys.sh,
                                       tenant-keys.tsv (generated, untracked)
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
