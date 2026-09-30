# AI Engineering: GenAI and Agentic Systems

The course repository for **AI Engineering: GenAI and Agentic Systems**. Six parts and a pre-work part, four projects, each shipped as a running service, on one domain: support-ticket intelligence for Northwind Cloud, a B2B SaaS company.

Northwind Cloud has just hired you as its AI engineer to make the support desk faster without making it dangerous.

## Start here

1. Pick your track and keep it for the whole course:

   | Track | What you need | Platform |
   | --- | --- | --- |
   | `aws` | a tenant on the cohort account, or your own account (solo) | SageMaker, Bedrock, AgentCore, CodePipeline (`deploy/aws`, CDK) |
   | `gcp` | a tenant on the cohort project, or your own project (solo) | the Agent Platform (formerly Vertex AI), Cloud Run, Cloud Build and Cloud Deploy (`deploy/gcp`, Terraform) |
   | `azure` | a tenant on the cohort subscription, or your own subscription (solo) | Azure Machine Learning, Microsoft Foundry, AI Search, API Management (`deploy/azure`, Bicep) |
   | `local` | a machine with 32 GB (16 GB with `SMALL=1`), no cloud account | MLflow, Kubeflow's local runner, Qdrant, Ollama, LiteLLM and more in Docker Compose (`deploy/local`) |

2. Install `uv` and Docker.
3. Clone this repository, then:

```bash
make setup
cp .env.example .env      # set NW_TRACK, and NW_TENANT with your gateway details on a cohort
make preflight            # one round trip per model role; paste the table into the cohort channel
```

   On the Local track, `make setup-local` and `make local-up` instead (see `deploy/local/README.md`).

4. Open the guide: https://techwithshadab.github.io/northwind-ai-engineering and start with the pre-work.

## Layout

| Path | What it is |
| --- | --- |
| `nw/` | The package. Every project lives here and imports from here |
| `nw/llm/` | The shared service layer in front of any model provider (the first part) |
| `nw/platform/` | The platform contract and one implementation per track |
| `nw/pipelines/` | Training pipelines: one step code, Kubeflow, SageMaker and Azure ML definitions |
| `sessions/session-NN/` | One README per part pointing at the guide and the files you edit |
| `tests/sessionNN/` | Acceptance tests per part. `make sessionNN` runs one part's tests |
| `notebooks/` | Exploration notebooks from the second part on. Never the deliverable |
| `data/` | Synthetic Northwind tickets, policy corpus, golden sets, the PII overlay, use cases and agent cards |
| `deploy/aws`, `deploy/gcp`, `deploy/azure`, `deploy/local` | The platform for each track; `deploy/COSTS-platform.md` is the cost sheet |
| `docs/` | The threat model, governance documents, decisions (`docs/adr`) and API snapshots |
| `scripts/` | Preflight, deploy, release and data tooling |

## Conventions

- Code asks for a model by role (`workhorse`, `judge`, `economy`), never by ID. Model IDs live in `nw/config.py` and `.env`.
- Model calls go through `nw/llm/` and platform calls through `nw/platform/`. The few other places that touch a cloud SDK (secrets, the ops store, screening, retrieval, telemetry, artifact download) import it inside the function, so a laptop without the SDK still runs.
- Every service emits structured JSON logs with a correlation ID, and meters its model spend.
- Tests never need a cloud account. Anything that spends money is marked `live`.
- The exercise tests are red until you do the exercise. `make test` and CI run only the tests the skeleton is expected to pass (`tests/skeleton-green.txt`); `make sessionNN` is how you check your own progress.
- Agent-written code is held to the same bar as anything else: it passes the tests or it does not go in.

## Solutions

After each part, its solution is published on the branch `solutions/session-NN`.

## Versioning

Everything that changes an answer has a version, and each lives in one place:

| What | Version | Where |
| --- | --- | --- |
| Code | git commit; a release is a `vX.Y.Z` tag cut with `make release VERSION=X.Y.Z` | `CHANGELOG.md`, the `release` workflow |
| Package | `pyproject.toml` `version`, read back as `nw.__version__`; every response carries `x-nw-version` | `nw/__init__.py` |
| API | `/v1/...` routes, `x-api-version` on every response, `api_version` in `/version`; the bare paths are deprecated aliases for one release | `nw/api.py`, snapshots in `docs/openapi/`, checked by `scripts/openapi_snapshot.py --check` |
| Images | base images pinned by digest in the `Dockerfile`; built images tagged `sha-<commit>` by the `image` workflow and `vX.Y.Z` by `release`, signed with cosign keyless | `Dockerfile`, `.github/workflows/image.yml` |
| Models and artifacts | a timestamped version directory per candidate with a model card, `latest` pointing at the promoted one; `/version` and `nw_*_model_info` report it | `artifacts/triage`, `artifacts/semantic`, the `promote` modules |
| Prompts | `name@hash` per registered prompt, on every answer and in every evaluation report | `nw/llm/prompts`, `make prompts` |
| Indexes | the manifest with the corpus hash, the embedder and the prompt versions at build; `nw_policy_index_stale` when the corpus moved on | `artifacts/policy/manifest.json`, `make check-index` |
| Agent | one hash over the system prompt, the tool specs and the model ids, on every trajectory | `nw/agent/version.py` |
| Data | `dataset_version` (CalVer) with sha256 and row counts per file; CI fails when data changes without it | `data/MANIFEST.json`, `python -m nw.data_manifest --check` |
| Infrastructure | the CDK app, the Terraform modules and the Bicep templates in git, pinned CLI and provider versions, synth, validate and build tests per track | `deploy/aws`, `deploy/gcp`, `deploy/azure`, `make session06` |

## Security

`docs/SECURITY.md` is the threat model: each threat, the control, and the file or stack where it lives. `docs/governance/` holds the DPIA, the risk register, datasheets, retention and erasure, and residency. The decisions behind them are in `docs/adr/`. Report a vulnerability as that document says, not in a public issue.

## Data and licence

Every ticket, account and policy document here is synthetic, generated for the course; nothing is real customer data. The ticket schema and label mix are modelled on the public Hugging Face dataset Tobi-Bueck/customer-support-tickets (CC BY-NC 4.0), of which no rows are shipped. See `data/README.md`.

The code is MIT licensed (`LICENSE`). The course material is for the cohort's use.

## Getting help

- The guide's Reference tab for each part explains the why; the step's "What to expect" block tells you what right looks like.
- `/session-check N` in your coding agent reads your checkout against the part's acceptance criteria.
- Open an issue on this repository for anything broken in the skeleton; bring anything else to the cohort channel.
