# deploy/gcp

Terraform for the Google Cloud track: one environment in one project, every learner a tenant
on it (ADR 0005, 0008, 0009, 0012). The platform runs on the Agent Platform (formerly Vertex AI):
Pipelines, Model Registry, endpoints, Model Monitoring, prompt management, RAG Engine and Agent
Engine, with Cloud Run for the services, LiteLLM on Cloud Run as the model gateway, Cloud Build
and Cloud Deploy for delivery, and Cloud Monitoring for the rest. The root is `platform/`; each
area of Google's GenAI and ML blueprint is a module:

| Module | What it creates | Where Terraform has no resource |
| --- | --- | --- |
| `data` | Buckets for data, artifacts and the pipeline root (CMEK optional), a BigQuery dataset and an external table over the tickets, a Dataplex catalog entry (off by default) | |
| `tracking` | A pipelines service account per tenant with least privilege, a Cloud Scheduler job per tenant that starts the weekly retraining pipeline (paused by default) | Experiments need no resource; the Model Registry is populated by the pipelines |
| `serving` | Per tenant: triage, semantic and policy on Cloud Run from the course images, reading the deployed version through `NW_MODEL_URI` | |
| `live` | The promoted target: a Vertex endpoint per live model with a traffic split, the live services' identity | Model Monitoring v2: `scripts/gcp_model_monitor.py` through the SDK |
| `prompts` | A RAG Engine corpus per tenant (RagManagedDb, or Vector Search per tenant with `rag_backend = "vector_search"`) | Prompt versions: `scripts/gcp_prompts.py` through `vertexai.preview.prompts`, stages in the artifacts bucket |
| `agents` | Agent Engine per tenant from the `nw-agent` image, one Model Armor template, a service account per tenant, the registry document `agents/agents.json` | Google's Agent Registry (preview) has only IAM resources in the provider; the document is the registry |
| `gateway` | LiteLLM on Cloud Run, master key in Secret Manager, Cloud SQL for virtual keys, a key with a budget and an identity service account per tenant | Key registration with the proxy: `scripts/gcp_gateway_keys.sh` |
| `delivery` | Artifact Registry (images plus a ghcr.io proxy), Cloud Build triggers on the GitHub repository (pull request, main), a Cloud Deploy pipeline with one target, a canary and a manual approval, the deployer identity | |
| `observability` | Notification channel, log-based metrics, alert policies, the platform dashboard, the budget | IAP, Security Command Center and Organization Policy are notes below |

Names: `<environment>-<tenant>-<kind>` everywhere (`northwind-alice-triage`), except service
account ids, which IAM caps at 30 characters and which use `nw-<tenant>-<kind>`. `mode = "solo"`
yields the one tenant `solo`. `live` is the promoted target's name and cannot be a tenant.

Verified against the `google` and `google-beta` 8.4.0 provider schemas
(`terraform providers schema -json`, 2026-09-29) and the provider docs; `terraform validate`
and a credential-free `terraform plan` on the fixtures run in `tests/platform/test_gcp_terraform.py`.

## Prerequisites

- A project with billing, `gcloud` logged in as an owner of it, Terraform 1.9 or later, Docker
  with buildx, `uv`.
- `NW_GCP_PROJECT`, `NW_BILLING_ACCOUNT`; optionally `NW_GCP_PROJECT_NUMBER` (looked up otherwise),
  `NW_ALERT_EMAIL`, `NW_ENVIRONMENT` (default `northwind`), `NW_GCP_RUN_REGION` (default
  `us-central1`).
- Model access: gpt-oss on Vertex Model Garden is on by default; Claude (the Judge) needs the
  model enabled once in Model Garden for the project.
- For the Cloud Build triggers: install the Cloud Build GitHub app on the repository owner, put
  a GitHub OAuth token in Secret Manager, and pass `NW_GITHUB_OWNER`,
  `NW_GITHUB_APP_INSTALLATION_ID`, `NW_GITHUB_TOKEN_SECRET_VERSION`. Without them the triggers
  are skipped and `make release-gcp` creates releases from the laptop.
- `uv sync --extra platform-gcp` for `nw/platform/gcp.py` and the scripts.

```bash
make images-gcp                       # build and push the five images (creates the repository if needed)
make validate-gcp                     # fmt, init, validate
make plan-gcp NW_TENANTS=alice,bob    # the plan, no credentials needed beyond the project number
make deploy-gcp NW_TENANTS=alice,bob  # apply after validate and plan; prints the next step
make keys-gcp                         # register every tenant key with the gateway (needs the database)
make tenants-gcp                      # what each tenant got: URLs, engine, corpus, secrets
make status-gcp                       # Cloud Run, Agent Engine, endpoints, pipeline runs, rollouts
make stop-gcp                         # scale to zero, undeploy live models, stop Cloud SQL
make start-gcp                        # the reverse, live endpoints stay empty until the drill
make destroy-gcp                      # terraform destroy, then the live services Cloud Deploy made
NW_MODE=solo make deploy-gcp          # one tenant named solo, in your own project
```

Read `deploy/COSTS-platform.md` first. The apply takes 15 to 25 minutes on a fresh project
(Cloud SQL and Agent Engine are the slow parts); `terraform plan` runs before every apply.

## The tenant workflow

What a learner does on the platform, in the order of the course. Cohort mode: the instructor
ran `make deploy-gcp` with every handle in `NW_TENANTS`; the learner sets `NW_TENANT=<handle>`
and impersonates `nw-<handle>-user@<project>.iam.gserviceaccount.com` (granted through
`tenant_members`). Solo mode: the learner is `solo` and owns the project.

1. Part 0: `NW_TRACK=gcp NW_TENANT=alice make preflight` reaches the gateway with the tenant
   key (`NW_GATEWAY_URL`, `NW_GATEWAY_KEY` from Secret Manager `northwind-alice-gateway-key`)
   and lists the tenant's resources with `make tenants-gcp`.
2. Project 1: `make pipeline-compile` writes the Kubeflow YAML (`triage.yaml` and the same
   pipeline as `retrain-triage.yaml`, the name the scheduler job reads);
   `make pipeline-upload-gcp` copies it to `gs://<artifacts bucket>/northwind-alice/pipelines/`
   (`make deploy-gcp` runs it for every tenant, and every submission uploads what it runs).
   The platform client submits it (`platform.pipelines.submit(tenant, "retrain-triage", {...})`) to
   `gs://<pipelines bucket>/northwind-alice` as `nw-alice-pipelines`. The pipeline's last step
   registers the artifact: model `northwind-alice-triage`, a new version with the alias
   `candidate`. The gate moves it to `approved`; `platform.endpoints.deploy(tenant, version)`
   rewrites `NW_MODEL_URI` on `northwind-alice-triage` (Cloud Run) and the service reloads.
   `/drift` on the service and the paused scheduler job (`scheduler_enabled = true` turns the
   weekly candidate on) close the loop.
3. Project 2: the same for `northwind-alice-semantic`.
4. Project 3: `scripts/gcp_prompts.py` registered every prompt at apply time; the learner's
   edits go through `platform.prompts.register` and `set_stage`. The policy corpus is imported
   with `platform.vectors.upsert(tenant, "policies", ...)` into the corpus
   `northwind-alice-policies`; `search` is `retrieval_query`.
5. Project 4: `platform.agents.deploy(tenant, image, env, version=...)` updates the reasoning
   engine `northwind-alice-agent` in place; `invoke` queries it through the `route` class method;
   `register` adds the agent card to `agents/agents.json`. Model Armor screens every prompt and
   response through `NW_MODEL_ARMOR_TEMPLATE`.
6. Capstone: the promotion drill below.

Every model call from a tenant's services, agent and notebooks goes through the gateway with
the tenant's key: `make keys-gcp` gave the key a budget of `tenant_budget_usd` per 30 days, and
`GET /spend/keys` with the master key is the per-tenant cost line.

## The promotion drill, inside the one project

Two artifacts promote, and the drill exercises both with a canary and a human approval:

**A model version into the live endpoint.** `platform.endpoints.deploy(tenant, version,
live=True, canary_percent=10)` deploys the approved version of `triage` on the Vertex endpoint
`northwind-live-triage` (id `100000`) at 10 percent of traffic; the previously serving version
keeps 90. The check is the dashboard's live row and `platform.endpoints.status(Tenant("live"),
"triage")`. Then `deploy(..., live=True, canary_percent=0)` moves everything, and
`registry.set_stage(tenant, "triage", version, Stage.LIVE, reason)` records the reason in
`registry/triage/stages.jsonl`. `scripts/gcp_model_monitor.py create` (rerun after the first
promotion, or `terraform apply` again) starts Model Monitoring on the endpoint; its anomalies
alert through `northwind: Model Monitoring anomaly on a live endpoint`.

**A service image into the live services.** `make release-gcp` resolves the images tagged with
the current git SHA to digests and creates a Cloud Deploy release on the pipeline
`northwind-live`. The rollout renders the manifests in `platform/delivery/` and deploys the
canary revision at `canary_percent` with automatic traffic control, then stops: the target
requires approval. `make approve-gcp` (or the console) approves and the rollout advances to
100 percent. `gcloud deploy rollouts list` is the audit trail. On a push to `main` the same
release is created by the `northwind-main` Cloud Build trigger, so the drill and the pipeline
are one path.

## Lower and higher environments

The course builds one environment. An organisation splits the same code into a shared
project (registry, gateway, observability, delivery), a lower project per team (tenants,
candidates) and a higher project (the live target), and the pattern in this repository moves
without a rewrite:

- **Environment name.** `environment` is the prefix of every name; `terraform apply` with
  `environment = "nwprod"` into another project is the higher environment. The tenant list is
  empty there: no learner works in the higher environment, only the promotion pipeline.
- **Second target.** Add a `google_clouddeploy_target` for the higher project's Cloud Run
  location as a second stage of the pipeline, after `northwind-live`, with `require_approval`
  and its own `execution_configs.service_account`: a deployer service account created in the
  higher project. The release then promotes from the lower target to the higher one with
  `gcloud deploy releases promote`, the same canary and approval per stage.
- **Deployment role.** The higher project grants its deployer to the shared project's builder
  through `roles/iam.serviceAccountUser` on the deployer and `roles/clouddeploy.releaser` on the
  pipeline; the higher project's registry and secrets are read through
  `roles/artifactregistry.reader` on the shared repository and `roles/secretmanager.secretAccessor`
  on the higher secrets. Nothing in the lower project can write to the higher one; the pipeline
  is the only path.
- **Model promotion across projects.** The Model Registry is per project. The promotion step
  copies the approved version (`aiplatform.Model.upload` with the same artifact URI into the
  higher project, alias `live`) or, when the organisation keeps one registry in the shared
  project, deploys from it into the higher endpoint with `roles/aiplatform.user` granted to the
  higher project's endpoint service account. Model Monitoring runs in the higher project.
- **Gateway.** One gateway in the shared project, keys per tenant and per environment, budgets
  per key; the higher environment's services carry a `live` key.
- **Data.** The tickets bucket and BigQuery dataset are per environment; CMEK keys live in a
  security project and are passed as `cmek_key`.

## Identity and security notes

- **Identity-Aware Proxy.** Cloud Run services take unauthenticated calls guarded by
  `x-api-key` (`public_services = true`) because the guide's curl needs it and the cohort has
  no Workspace. With a Workspace: set `public_services = false`, put an external Application
  Load Balancer with IAP in front of the services (about 18 USD per month, `deploy/COSTS.md`),
  and grant `roles/iap.httpsResourceAccessor` per tenant group. The gateway stays key-guarded.
- **Cloud Identity.** Tenants are service accounts (`nw-<tenant>-user`); `tenant_members` maps
  a learner's Google account to the right to impersonate one. No user keys are created.
- **Security Command Center.** The Standard tier is free and on for every project in an
  organisation; its findings for this platform are public buckets (prevented) and open Cloud
  Run services (the API key guard). The Premium tier is an organisation decision.
- **Organization Policy.** The `organization_policies` output lists the constraints a
  platform team sets at the folder (service account key creation off, uniform bucket access,
  public access prevention, resource locations, Cloud SQL public IP off). The course applies
  none: it runs in one project without an organisation.
- **Secrets.** Every key is a Secret Manager secret injected by reference; `terraform output
  -raw api_key` prints the cohort key once.

## Scripts and where the API is

| Script | Called by | API |
| --- | --- | --- |
| `scripts/gcp_prompts.py` | `modules/prompts` (per tenant), `make prompts-gcp` | `vertexai.preview.prompts.create_version` (google-cloud-aiplatform), stages in the bucket |
| `scripts/gcp_model_monitor.py` | `modules/live` (per live endpoint) | `vertexai.resources.preview.ml_monitoring.ModelMonitor.create` and `create_schedule` |
| `scripts/gcp_gateway_keys.sh` | `make keys-gcp` | LiteLLM `POST /key/generate`, `POST /key/update`, `GET /key/info` |
| `scripts/deploy_gcp.sh release` | `make release-gcp`, `cloudbuild-main.yaml` | `gcloud deploy releases create --images` by digest |
