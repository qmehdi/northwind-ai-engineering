# deploy/gcp

Terraform for the Google Cloud track: one environment in one project, every learner a tenant
on it (ADR 0005, 0008, 0009, 0012). The platform runs on the Agent Platform (formerly Vertex AI):
Pipelines, Model Registry, endpoints, Model Monitoring, prompt management, RAG Engine and Agent
Runtime (formerly Agent Engine; the API resource is still `ReasoningEngine`), with Cloud Run for the services, LiteLLM on Cloud Run as the model gateway, Cloud Build
and Cloud Deploy for delivery, and Cloud Monitoring for the rest. The root is `platform/`; each
area of Google's GenAI and ML blueprint is a module:

| Module | What it creates | Where Terraform has no resource |
| --- | --- | --- |
| `identity` | The tenant identity `nw-<tenant>-user`, the custom roles (tenant, tenant engine, live deployer, pipelines, agent runtime, RAG reader), a service API key per tenant, the secret class tag | |
| `data` | Buckets for data, artifacts and the pipeline root (CMEK optional) with a managed folder per tenant and lifecycle deletes, a BigQuery dataset with an external table over the tickets, the capture dataset (90 days), a Dataplex catalog entry (off by default) | |
| `tracking` | A pipelines service account per tenant, a Cloud Scheduler job per tenant that starts the weekly retraining pipeline (paused by default) | Experiments need no resource; the Model Registry is populated by the pipelines |
| `serving` | Per tenant: triage, semantic and policy on Cloud Run from the course images, reading the deployed version through `NW_MODEL_URI`, and the private MCP server `<environment>-<tenant>-mcp` | |
| `live` | The promoted target: a Vertex endpoint per live model with a traffic split, the live services' identity, the drill's grant on the endpoints | Model Monitoring v2: `scripts/gcp_model_monitor.py` through the SDK |
| `prompts` | A RAG Engine corpus per tenant (RagManagedDb, or Vector Search per tenant with `rag_backend = "vector_search"`) | Prompt versions: `scripts/gcp_prompts.py` through `vertexai.preview.prompts`, stages in the artifacts bucket; setting RagManagedDb to Unprovisioned on destroy |
| `agents` | Agent Runtime per tenant from the `nw-agent` image, one Model Armor template, a service account per tenant, the registry document `agents/agents.json` | Google's Agent Registry (preview) has only IAM resources in the provider; the document is the registry |
| `gateway` | LiteLLM on Cloud Run pinned by digest, master key in Secret Manager, Cloud SQL for virtual keys, a gateway key per tenant and one for the live services | Key registration with the proxy: `scripts/gcp_gateway_keys.sh` |
| `delivery` | Artifact Registry with immutable tags (images plus a ghcr.io proxy), Cloud Build triggers (pull request on an unprivileged account, main), a Cloud Deploy pipeline with one target, an approval and a canary, the deployer identity | |
| `observability` | Notification channel, log-based metrics (drift, quality, anomalies, pipeline errors, budget), alert policies, the platform dashboard, the budget | IAP, Security Command Center and Organization Policy are notes below |
| `guardrails` | Data Access audit logs for Secret Manager and the Agent Platform, a 400 day audit log bucket, quota caps on Agent Platform compute, and with an organisation the IAM deny policy on the platform secrets and a machine type constraint | |

Names: `<environment>-<tenant>-<kind>` everywhere (`northwind-alice-triage`), except service
account ids, which IAM caps at 30 characters and which use `nw-<tenant>-<kind>`. `mode = "solo"`
yields the one tenant `solo`. `live` is the promoted target's name and cannot be a tenant, nor
can `platform`, `gateway`, `baselines`, `agents`, `monitoring`, `clouddeploy` or `audit`.

Verified against the `google` and `google-beta` 8.4.0 provider schemas
(`terraform providers schema -json`, 2026-09-30) and the provider docs; `terraform validate`
and a credential-free `terraform plan` on the fixtures run in `tests/platform/test_gcp_terraform.py`.
Two things only an apply can confirm, and the validation run checks first: the Agent Platform
quota metric names in `modules/guardrails` (`custom_model_training_cpus`, `custom_model_serving_cpus`,
`custom_model_{training,serving}_nvidia_<gpu>_gpus`) and that every permission in the custom
roles is allowed in a custom role (`gcloud iam roles describe` after the first apply).

## Prerequisites

- A project with billing, `gcloud` logged in as an owner of it, Terraform 1.11 or later (the
  secrets are write-only), Docker with buildx, `uv`.
- `NW_GCP_PROJECT`, `NW_BILLING_ACCOUNT`; optionally `NW_GCP_PROJECT_NUMBER` (looked up otherwise),
  `NW_ALERT_EMAIL`, `NW_ENVIRONMENT` (default `northwind`), `NW_GCP_RUN_REGION` (default
  `us-central1`), `NW_TENANT_MEMBERS` (cohort: `alice=alice@example.com,bob=bob@example.com`),
  `NW_GCP_ORGANIZATION_ID` (only when the project sits in an organisation where you hold
  `roles/iam.denyAdmin` and `roles/orgpolicy.policyAdmin`).
- Terraform state: `make state-bucket-gcp` once creates `gs://<project>-<environment>-tfstate`
  with object versioning (`NW_TF_STATE_KMS_KEY` adds CMEK); every other command initialises
  against it with the prefix `<environment>/platform`. Solo mode on one laptop may keep local
  state with `NW_TF_STATE=local` (the script writes `platform/backend_override.tf`). The state
  holds no secret value: every generated key and password is write-only.
- Model access: gpt-oss on Vertex Model Garden is on by default; Claude (the Judge) needs the
  model enabled once in Model Garden for the project.
- For the Cloud Build triggers: install the Cloud Build GitHub app on the repository owner, put
  a GitHub OAuth token in Secret Manager, and pass `NW_GITHUB_OWNER`,
  `NW_GITHUB_APP_INSTALLATION_ID`, `NW_GITHUB_TOKEN_SECRET_VERSION`. Without them the triggers
  are skipped and `make release-gcp` submits the same build from the laptop.
- `uv sync --extra platform-gcp` for `nw/platform/gcp.py` and the scripts.

```bash
make state-bucket-gcp                 # once: the versioned state bucket
make images-gcp                       # build and push the six images under the git SHA tag
make validate-gcp                     # fmt, init, validate
make plan-gcp NW_TENANTS=alice,bob    # the plan
make deploy-gcp NW_TENANTS=alice,bob NW_TENANT_MEMBERS=alice=alice@example.com,bob=bob@example.com
make keys-gcp                         # register every tenant key and the live key with the gateway
make tenants-gcp                      # what each tenant got: identity, URLs, engine, corpus, secrets
make status-gcp                       # Cloud Run, Agent Runtime, endpoints, pipeline runs, rollouts
make stop-gcp                         # scale to zero, undeploy live models, stop Cloud SQL
make start-gcp                        # the reverse, live endpoints stay empty until the drill
make destroy-gcp                      # undeploy, terraform destroy, RagManagedDb to Unprovisioned, live services
NW_MODE=solo NW_TF_STATE=local make deploy-gcp   # one tenant named solo, in your own project
```

Read `deploy/COSTS-platform.md` first. The apply takes 15 to 25 minutes on a fresh project
(Cloud SQL and Agent Runtime are the slow parts); `terraform plan` runs before every apply.

## The tenant workflow

What a learner does on the platform, in the order of the course. Cohort mode: the instructor
ran `make deploy-gcp` with every handle in `NW_TENANTS` and every Google account in
`NW_TENANT_MEMBERS`; the learner sets `NW_TENANT=<handle>` and works as the tenant identity:

```bash
gcloud config set auth/impersonate_service_account nw-alice-user@<project>.iam.gserviceaccount.com
```

Solo mode: the learner is `solo` and owns the project; impersonating `nw-solo-user` rehearses
the cohort's permissions.

What the tenant identity may do, and nothing more: submit pipeline runs as `nw-<tenant>-pipelines`
and upload and alias models (custom role `<environment>_tenant`); read and write its own managed
folder `<environment>-<tenant>/` in the artifacts and pipelines buckets and read `baselines/`,
`agents/` and the tickets; update, invoke and act as its own three services; invoke its MCP
server; update and query its own reasoning engine (a grant on that engine); deploy to and move
traffic on the live endpoints for the drill; read its own API key and gateway key; read logs,
metrics and the tickets table. It cannot call a model directly (no `endpoints.predict`: every
model call goes through the gateway with the tenant's key and budget), create or delete
reasoning engines, start custom jobs, notebooks or tuning jobs, or touch another tenant's
folders, services, engine or keys.

1. Part 0: `NW_TRACK=gcp NW_TENANT=alice make preflight` reaches the gateway with the tenant
   key (`NW_GATEWAY_URL`, `NW_GATEWAY_KEY` from Secret Manager `northwind-alice-gateway-key`)
   and lists the tenant's resources with `make tenants-gcp`. The tenant's services take the
   tenant's own API key, `northwind-alice-api-key`.
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
   weekly candidate on) close the loop. The gates read `gs://<artifacts>/baselines/`, which only
   the platform writes.
3. Project 2: the same for `northwind-alice-semantic`.
4. Project 3: `scripts/gcp_prompts.py` registered every prompt at apply time; the learner's
   edits go through `platform.prompts.register` and `set_stage`. The policy corpus is imported
   with `platform.vectors.upsert(tenant, "policies", ...)` into the corpus
   `northwind-alice-policies`; `search` is `retrieval_query`, which the policy service runs with
   the RAG reader role.
5. Project 4: `platform.agents.deploy(tenant, image, env, version=...)` updates the reasoning
   engine `northwind-alice-agent` in place (Terraform created it; a tenant cannot create one);
   `invoke` queries it through the `route` class method. The agent's card goes to
   `northwind-alice/agents/`; the platform merges cards into `agents/agents.json`. Model Armor
   screens every prompt and response through `NW_MODEL_ARMOR_TEMPLATE`. The MCP server
   `northwind-alice-mcp` serves the same tools at `/mcp`; it is private and only
   `nw-alice-agent` (and the learner, for inspection) may call it, with a Google ID token.
6. Capstone: the promotion drill below.

Every model call from a tenant's services, agent and notebooks goes through the gateway with
the tenant's key: `make keys-gcp` gave the key a budget of `tenant_budget_usd` per 30 days, and
`GET /spend/keys` with the master key is the per-tenant cost line.

Residual sharing inside one project, stated rather than hidden: Model Registry models, prompt
versions and RAG corpora are project-level objects without resource-level IAM, so a tenant
with the tenant role could alter another tenant's by name. Names carry the tenant prefix, the
Data Access audit log records every call, and the course accepts this for a teaching project;
an organisation gives each team its own project (below).

## The promotion drill, inside the one project

Two artifacts promote, and the drill exercises both with a canary and a human decision:

**A model version into the live endpoint.** `platform.endpoints.deploy(tenant, version,
live=True, canary_percent=10)` deploys the approved version of `triage` on the Vertex endpoint
`northwind-live-triage` (id `100000`) at 10 percent of traffic; the previously serving version
keeps 90. The check is the dashboard's live row and `platform.endpoints.status(Tenant("live"),
"triage")`. Then `deploy(..., live=True, canary_percent=0)` moves everything, and
`registry.set_stage(tenant, "triage", version, Stage.LIVE, reason)` records the reason in
`registry/triage/stages.jsonl`; `platform.endpoints.promote(Tenant("live"), "triage")` does the
same finish, and `rollback` sends everything back to the stable model and undeploys the canary.
Nothing rolls back on its own on this track: the alert policies (server errors on the services,
drift, the quality signals, Model Monitoring anomalies) notify the channel and a person acts; no
alert policy counts the Vertex endpoint's server errors or any service's latency, which are
dashboard panels. `scripts/gcp_model_monitor.py create` (rerun after the first
promotion, or `terraform apply` again) starts Model Monitoring on the endpoint; its anomalies
alert through `northwind: Model Monitoring anomaly on a live endpoint`. The endpoints log a 10
percent sample of requests to the capture dataset, whose tables expire after 90 days.

**A service image into the live services.** `make release-gcp` submits
`platform/delivery/cloudbuild-main.yaml` to Cloud Build as the builder account: it builds the
images from the checkout under the git SHA tag, signs every digest with cosign keyless as the
builder, verifies the signatures, and creates a Cloud Deploy release on the pipeline
`northwind-live` by digest. On a push to `main` the `northwind-main` trigger runs the same
build, so the drill and the pipeline are one path and nothing a laptop built reaches live.
The rollout first waits for approval (the target requires it, before any traffic moves);
`make approve-gcp` approves, the canary revision takes `canary_percent` with automatic traffic
control, and the rollout pauses before the stable phase; `make approve-gcp` again advances it
to 100 percent. The AWS and Azure tracks put the human after the canary only; here the same
person approves the rollout and then advances it. `gcloud deploy rollouts list` is the audit
trail. The live services are private: they reach each other as `northwind-live` (a conditional
`run.invoker` on `northwind-live-*`), call the gateway with the live key
`northwind-live-gateway-key` (registered by `make keys-gcp`), and their URLs are Cloud Run's
deterministic `https://<service>-<project number>.<region>.run.app`. The live agent runs on
Cloud Run, not Agent Runtime: Cloud Deploy promotes Cloud Run services, so the tenant agents are
on Agent Runtime and the promoted one is a service.

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
  per key; the higher environment's services carry a `live` key, as the live services do here.
- **Data.** The tickets bucket and BigQuery dataset are per environment; CMEK keys live in a
  security project and are passed as `cmek_key`.

## Operations state and the approval gate

The agents and the policy services keep their durable state in the artifacts bucket
(`NW_OPS_STORE=gs://<artifacts bucket>`, output `ops_store`, `nw/agent/opstore.py`), under the
owner's folder: `<environment>-<owner>/trajectories/` (every run, with its proposed actions),
`/feedback/` (policy verdicts) and `/approvals/` (claim markers, approval records, the
escalation queue), each a nested managed folder with the retention of the table in
`docs/governance`. A runtime identity (a tenant's agent on Agent Runtime, its policy service, the
live services) may write `trajectories/` and `feedback/` of its own owner and reads the rest of
the folder; it holds no write role on `approvals/`. The approvers do: the tenant identity for
its own folder, and `<environment>-approvers` for `live`, which the members in `NW_APPROVERS`
(`user:` or `group:`) impersonate to run `make approve` with `NW_TENANT=live`. That is the
platform half of the approval gate (ADR 0005): an agent that got past the loop's gate still
cannot queue an escalation. The Agent Runtime resolver sets `NW_RUNTIME_AUTH=platform` (the
engine authorises every query with IAM) and reaches its tools through the tenant's private MCP
service with an ID token (`NW_TOOL_BACKEND=mcp`, `NW_MCP_AUTH=google-id-token`).

## Identity and security notes

- **Secrets.** Every key is a Secret Manager secret injected by reference. The values come from
  ephemeral random passwords into write-only arguments (`secret_data_wo`, `password_wo`), so no
  key is in the state or a plan file; `secrets_generation` rotates them on the next apply (the
  gateway salt key never rotates), `scripts/rotate_key.sh` adds a version out of band. Read one
  with `gcloud secrets versions access latest --secret <id>`.
- **Agent Runtime and secrets.** Engines carry no `secret_env`: the Reasoning Engine service
  agent is one per project and would read any secret an engine names. The engine's own service
  account reads its tenant's API key and gateway key at start (`NW_API_KEY_SECRET_NAME`,
  `NW_GATEWAY_KEY_SECRET_NAME`); the service agent holds no Secret Manager role.
- **Deny policy.** With `organization_id` set, an IAM deny policy keeps every secret tagged
  `<project>/<environment>-secret-class=platform` (gateway master key, salt, config, database
  URL) from every tenant and workload identity, and every secret from the Reasoning Engine
  service agent. Without an organisation the allow side is the control: only the gateway's
  account holds those secrets.
- **Compute limits.** Quota overrides cap the Agent Platform in the region: zero GPUs for
  training and serving, `training_cpu_quota` custom job CPUs (8 per tenant, at least 16) and
  `serving_cpu_quota` serving CPUs. With an organisation, the custom constraint
  `custom.<environment>JobMachineTypes` also denies custom jobs outside `allowed_machine_types`
  or with accelerators. Tenants cannot start custom jobs themselves; the pipelines identity can,
  inside the quota.
- **Audit.** Data Access audit logs are on for Secret Manager and the Agent Platform (who read
  which secret, who called which model or engine); every audit log entry is routed to the log
  bucket `<environment>-audit`, kept 400 days.
- **Retention.** Objects are deleted, not only moved: operational data (`capture/`,
  `trajectories/`, `traces/`, `feedback/` in a tenant folder, `monitoring/`, pipeline roots) at
  90 days, `audit/` and `approvals/` at 400, noncurrent artifact versions at 30; the capture
  dataset expires tables after 90 days; the gateway keeps spend logs 90 days
  (`maximum_spend_logs_retention_period`; confirm the proxy honours it on the pinned release).
  The gateway database keeps no backups.
- **Supply chain.** The LiteLLM gateway image is pinned by digest (`gateway_image`, the release
  the Local track pins); course image tags are immutable and never `latest`; live images are
  signed and verified before release. Admission-time enforcement (Binary Authorization with a
  Cloud KMS attestor on the live services) is the production step the course names and does
  not build.
- **Pull requests.** The pull request trigger builds as `<environment>-pr-checks`, which can only
  write build logs: code from a pull request never runs with the builder's rights.
- **Residency.** EU accounts route to `eu/<role>` models (nw/config.py). Claude is served in the
  `eu` multi-region (`eu/judge` on the gateway); gpt-oss has no EU endpoint on Google, so the EU
  Workhorse and Economy routes are refused until an operator deploys gpt-oss to an EU endpoint,
  adds `eu/workhorse` and `eu/economy` to `gateway_models` and sets `NW_MODEL_EU_WORKHORSE` and
  `NW_MODEL_EU_ECONOMY`.
- **Identity-Aware Proxy.** Tenant services take unauthenticated calls guarded by the tenant's
  `x-api-key` (`public_services = true`) because the guide's curl needs it and the cohort has
  no Workspace. With a Workspace: set `public_services = false` (the tenant identity, the MCP
  server and the agent stay invokers), put an external Application Load Balancer with IAP in
  front (about 18 USD per month, priced in `docs/archive/COSTS.md`), and grant
  `roles/iap.httpsResourceAccessor` per tenant group. The gateway stays key-guarded; the MCP
  servers and the live services are private already.
- **Cloud Identity.** Tenants are service accounts (`nw-<tenant>-user`); `tenant_members` maps
  a learner's Google account to the right to impersonate one. No user keys are created.
- **Security Command Center.** The Standard tier is free and on for every project in an
  organisation; its findings for this platform are public buckets (prevented) and open Cloud
  Run services (the API key guard). The Premium tier is an organisation decision.
- **Organization Policy.** The `organization_policies` output lists the constraints a
  platform team sets at the folder (service account key creation off, uniform bucket access,
  public access prevention, resource locations, Cloud SQL public IP off). The course applies
  the machine type constraint when it has an organisation and lists the rest.
- **Destroy.** `make destroy-gcp` undeploys the live endpoints' models (an endpoint with
  deployed models cannot be deleted), destroys the platform, sets the project's RagManagedDb
  tier to Unprovisioned (the Basic tier otherwise bills every hour; `rag_unprovision_on_destroy
  = false` when another environment in the project still uses it), and deletes the live
  services Cloud Deploy made. The state bucket stays.

## Scripts and where the API is

| Script | Called by | API |
| --- | --- | --- |
| `scripts/gcp_prompts.py` | `modules/prompts` (per tenant), `make prompts-gcp` | `vertexai.preview.prompts.create_version` (google-cloud-aiplatform), stages in the bucket |
| `scripts/gcp_model_monitor.py` | `modules/live` (per live endpoint) | `vertexai.resources.preview.ml_monitoring.ModelMonitor.create` and `create_schedule` |
| `scripts/gcp_gateway_keys.sh` | `make keys-gcp` | LiteLLM `POST /key/generate`, `POST /key/update`, `GET /key/info` |
| `scripts/deploy_gcp.sh release` | `make release-gcp` | `gcloud builds submit` of `cloudbuild-main.yaml`, which runs `gcloud deploy releases create --images` by digest |
| `scripts/deploy_gcp.sh approve` | `make approve-gcp` | `gcloud deploy rollouts approve`, then `gcloud deploy rollouts advance --phase-id stable` |
| `modules/prompts` destroy | `terraform destroy` | `updateRagEngineConfig` with `ragManagedDbConfig.unprovisioned` |
| `modules/live` destroy | `terraform destroy` | `gcloud ai endpoints undeploy-model` per deployed model |
