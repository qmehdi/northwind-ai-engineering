# deploy/azure

Bicep for the Azure track: one environment in one resource group, every learner a tenant on it
(ADR 0008, 0009, 0012, 0013). Training, the model registry and the online endpoints run on Azure
Machine Learning; models, prompts' guardrail, agents and evaluations on Microsoft Foundry;
retrieval on Azure AI Search; the model gateway is API Management's AI gateway (LiteLLM on
Container Apps behind a parameter); the services run on Container Apps; delivery is Azure
Pipelines (GitHub Actions with OIDC as the documented alternative). The root is `main.bicep`;
each area of the reference architecture is a module in `modules/`, built on Azure Verified
Modules wherever the AVM index has one:

| Module | What it creates | AVM | What needed a script instead |
| --- | --- | --- | --- |
| `observability` | Log Analytics (1 GB a day cap), workspace-based Application Insights, the action group, the budget, the platform workbook, the `drift_alert` log search alert | workspace, component, action group, scheduled query rule | Budget and workbook are native (no resource group AVM for budgets, no workbook module) |
| `data` | The lake: Storage with hierarchical namespace, containers `data`, `artifacts`, `pipelines`, `baselines`, soft delete 7 days; the workspace's flat storage account; Microsoft Purview (`purview = true`, off) | storage account | The tickets and the two production summaries are blobs: `deploy_azure.sh deploy` uploads them |
| `tracking` | Azure ML workspace with its Key Vault (RBAC), Application Insights, storage and ACR; the lake's containers as identity-based datastores; the CPU cluster (0 to 4 nodes); the pipelines environment; a user-assigned identity per owner; the tenant workspace role and the endpoint role (custom) | vault, registry, user-assigned identity, ML workspace | |
| `retrieval` | Azure AI Search, Entra ID only, semantic ranker on the free plan; Basic up to 15 owners, Standard S1 above (Basic holds 15 indexes) | search service | Indexes are data plane: `deploy_azure.sh indexes` creates `northwind-<owner>-policies` from `search/policies-index.json` and grants its roles on that index |
| `foundry` | The Foundry resource and the project `northwind-project`; deployments `gpt-oss-120b` (Workhorse), `mistral-small-2503` (Economy, Mistral Small 3.1 through Azure Marketplace), `claude-opus-5` (Judge), `text-embedding-3-small` (the indexes' vectorizer); the content filter policy with Prompt Shields on every deployment; the project's connections to Application Insights and AI Search | cognitive services account | Foundry evaluation runs and agent versions are created in the project by `nw/platform/azure.py` |
| `gateway` | API Management Basic v2 with two APIs (`/openai`, `/anthropic`), `llm-token-limit` and `llm-emit-token-metric` per tenant subscription, a backend pool with a circuit breaker per API, managed identity to Foundry, a subscription per owner whose key lands in Key Vault; or LiteLLM on Container Apps with PostgreSQL | API Management service, container app, PostgreSQL flexible server | LiteLLM keys are registered with the proxy by `deploy_azure.sh keys` |
| `tenant` (per owner) | Online endpoints `nw-<owner>-triage-<scope>` and `-semantic-<scope>`; the weekly retraining schedule (disabled); every role of the owner | | |
| `agents` (per owner) | Container Apps `northwind-<owner>-policy`, `-agent` (external) and `-mcp` (internal only); the live policy and agent apps run in multiple-revision mode for the traffic split | container app, managed environment | |
| `serving` | Metric alerts on the live endpoint (5xx, p95) and on the live apps (5xx): what the canary is judged by | metric alert | |
| `delivery` | The `northwind-deployer` identity with federated credentials for Azure Pipelines and GitHub Actions, AcrPush, the deployer custom role | user-assigned identity | Approvals are environment checks in Azure DevOps (or GitHub environment rules), not ARM |
| `defender` | Defender for Containers at subscription scope (`defenderForContainers = true`, off) | | |
| `admin` | The platform owner's data plane roles (lake, search, Key Vault, Foundry) for the uploads and indexes | | |

Agents run in two places, split by owner (the same split is documented on the `AgentRuntime`
class in `nw/platform/azure.py`):

- **Tenant agents are Foundry hosted agents.** `platform.agents.deploy` creates a version of
  `northwind-<tenant>-agent` in the Foundry project from the `nw-agent` image (the project's
  identity pulls from the registry), and `platform.agents.register` writes its card with
  runtime `foundry-hosted-agent` into the registry document `agents/agents.json`. A hosted agent
  endpoint "serves one version at a time and routes 100% of its traffic to that version.
  Traffic splitting between versions isn't supported" (learn.microsoft.com, 2026-09-29), which
  a tenant does not need.
- **The live agent is a Container Apps revision with a traffic split.** `northwind-live-agent`
  runs in multiple-revision mode; `make release-azure` adds a revision at 10 percent and
  `make approve-azure` moves it to 100. It is registered too: `NW_TENANT=live make agent-cards
  PUSH=1` after the approval writes its card with runtime `container-apps` and the app's name
  into the same registry document. `platform.agents.deploy` refuses the live owner, so the live
  agent has one runtime only.

Every tenant also gets `-policy`, `-agent` and `-mcp` Container Apps from the template: the
service layer the parts call over HTTP, with the MCP server reachable only inside the
environment. The Foundry Agent Service is used in its basic setup (no capability host; the
standard setup with your own Cosmos DB, Storage and Search through a project capability host is
an organisation's choice).

Names: `<environment>-<owner>-<kind>` wherever Azure allows hyphens (`northwind-alice-agent`);
`nw<kind><suffix>` where it does not (storage `nwdata<suffix>`, `nwml<suffix>`, Key Vault
`nwkv<suffix>`, registry `nwacr<suffix>`); `<suffix>` is six characters of
`uniqueString(resource group id, environment)` and also ends every globally unique host name
(Foundry, APIM, Search). Online endpoint names are unique per region across every customer and
at most 32 characters, so they take the short form `nw/platform/azure.py` computes:
`nw-<owner>-<kind>-<scope>`, `<scope>` being five hex characters of SHA-256 of subscription,
resource group and environment (`deploy_azure.sh` passes it). Tenants are 2 to 12 lowercase
letters and digits; `live` is the promoted target's owner and cannot be a tenant.

Verified on 2026-09-29: resource properties against the Bicep 0.47.16 type index (the API
versions are the newest it carries: Cognitive Services 2026-07-01, Machine Learning 2026-05-01,
Container Apps 2026-01-01, API Management 2024-05-01), the Azure Verified Modules against the
public registry (`mcr.microsoft.com/v2/bicep/avm/res/<module>/tags/list`) and the AVM index
(`Azure/Azure-Verified-Modules`, `BicepResourceModules.csv`), the model formats and versions
against the Foundry model pages, the Claude deployment shape against `Azure-Samples/claude`.
`az bicep build` and `az bicep lint` run clean (`make build-azure`); `tests/platform/
test_azure_bicep.py` compiles the template and evaluates every resource name for a cohort and a
solo fixture without calling Azure.

## Prerequisites

- A subscription with a pay-as-you-go payment method. Claude (the Judge) and Mistral Small
  (the Economy role) on Foundry are sold through Azure Marketplace, and free trial, student,
  sponsored credit-only and CSP subscriptions cannot deploy Claude (Foundry Claude pages,
  2026-09-22). Owner on the subscription, or Contributor plus
  User Access Administrator, for the role assignments and the Marketplace terms.
- Region: `eastus2` (the default). It has Claude Opus 5 as Global Standard, mistral-small-2503,
  API Management v2, hosted agents, Azure ML, AI Search and Container Apps; `swedencentral` is
  the European alternative. gpt-oss-120b is labelled Preview and its region table does not list
  it; check the Foundry catalogue for the region in the delivery week.
- Quota, in thousands of tokens per minute per deployment (`models` parameter): gpt-oss-120b
  100, mistral-small-2503 100, claude-opus-5 20, text-embedding-3-small 150. Ask for more in the
  Foundry portal (Quotas) before a cohort; a 25-learner session peaks near 100 for the Workhorse.
  Azure ML: 8 vCPUs of DSv2 for the cluster and one live DS3_v2, 2 vCPUs of FSv2 per tenant
  endpoint that holds a deployment (Azure ML reserves 20 percent more during upgrades).
- The Azure CLI with the `ml` and `containerapp` extensions, Docker with buildx, `uv`.
  `make setup-azure` installs the SDKs and the Bicep CLI (`az bicep install`, or the release
  binary into `~/.azure/bin` when the Azure CLI is missing; no administrator rights needed).
- `az login` as the platform owner. `deploy_azure.sh` grants the signed-in user the data plane
  roles it needs (`adminObjectId`).

```bash
make setup-azure                                  # SDKs and the Bicep CLI
make build-azure                                  # bicep build and lint, no Azure call
NW_TENANTS=alice,bob make what-if-azure           # what would change (reads the subscription)
NW_TENANTS=alice,bob NW_ALERT_EMAIL=you@example.com make deploy-azure
make images-azure                                 # build and push; then deploy again for the images
NW_TENANTS=alice,bob make deploy-azure
make tenants-azure                                # what each tenant got
make tenants-azure ACTION=add TENANT=carol        # a late learner
make status-azure
make stop-azure                                   # delete online deployments, idle the apps
make destroy-azure                                # the resource group, soft-deleted names, custom roles
NW_MODE=solo make deploy-azure                    # one tenant named solo, in your own subscription
```

Environment: `NW_MODE`, `NW_TENANTS`, `NW_ENVIRONMENT` (default `northwind`),
`NW_AZURE_RESOURCE_GROUP` (default `rg-<environment>`), `NW_AZURE_LOCATION` (default `eastus2`),
`NW_ALERT_EMAIL`, `NW_BUDGET_USD`, `NW_GATEWAY_KIND=apim|litellm`, `NW_APIM_SKU`
(`BasicV2`, `Developer`, `StandardV2`), `NW_SEARCH_SKU`, `NW_TENANT_USERS=alice=<object id>,...`,
`NW_RETRAIN_ENABLED`, `NW_DEFENDER`, `NW_PURVIEW`, `NW_GITHUB_REPOSITORY`, `NW_ADO_ISSUER`,
`NW_ADO_SUBJECT`. A first deploy takes 20 to 35 minutes (API Management v2 and the model
deployments are the slow parts); the apps start on a public placeholder image until
`make images-azure` has pushed the course images, and the next deploy moves them over.

`make deploy-azure` writes `outputs.json` here: the thirteen `NW_AZURE_*` keys
`nw/platform/azure.py` reads, plus the tenants, apps, endpoint scope, embedding deployment,
content filter policy and deployer identity. `outputs.json` and `.env` are the two files a
learner needs.

## The tenant workflow

In cohort mode the instructor ran `make deploy-azure` with every handle in `NW_TENANTS` and each
learner's Entra object id in `NW_TENANT_USERS`, so the learner signed in with `az login` holds the
same roles as the tenant identity. Solo mode: the learner is `solo` and owns the subscription.

| Resource | Name | Made by |
| --- | --- | --- |
| Identity | `northwind-<tenant>-id` | the deployment |
| Registered models, experiments | `northwind-<tenant>-triage`, `-semantic` | the tenant through `nw/platform/azure.py` |
| Pipeline jobs | experiment `northwind-<tenant>-<pipeline>`, run as the tenant identity | the tenant |
| Retraining schedule | `northwind-<tenant>-retrain-triage` (disabled until `NW_RETRAIN_ENABLED=true`) | the deployment |
| Online endpoints | `nw-<tenant>-triage-<scope>`, `nw-<tenant>-semantic-<scope>` | the deployment; deployments by the tenant |
| Search index | `northwind-<tenant>-policies` | `deploy_azure.sh`; filled by `VectorStore.upsert` |
| Gateway key | APIM subscription `northwind-<tenant>`, Key Vault `northwind-<tenant>-gateway-key` | the deployment |
| Apps | `northwind-<tenant>-policy`, `-agent`, `-mcp` | the deployment |
| Blob prefixes | `artifacts/northwind-<tenant>/`, `pipelines/northwind-<tenant>/` | the tenant; nothing outside them (ABAC) |

The learner's `.env`:

```bash
NW_TRACK=azure
NW_TENANT=alice
NW_ENVIRONMENT=northwind
NW_GATEWAY_KEY=<az keyvault secret show --vault-name <NW_AZURE_KEY_VAULT> -n northwind-alice-gateway-key --query value -o tsv>
NW_AZURE_PIPELINE_IDENTITY_CLIENT_ID=<identity_client_id from make tenants-azure>
```

with the instructor's `outputs.json` copied to `deploy/azure/`. The gateway URL is not in the
file: `nw.config.Settings` reads `NW_AZURE_APIM_GATEWAY_URL` from `outputs.json` on the azure
track, and `NW_GATEWAY_KEY` is the tenant's APIM subscription key. Leave `NW_GATEWAY_URL` unset:
it names the LiteLLM gateway, takes LiteLLM's request shape, and is an output only when the
platform was deployed with `NW_GATEWAY_KIND=litellm`; then set it from `outputs.json` and the
key is the LiteLLM virtual key. In the order of the course:

1. Part 0: `NW_TRACK=azure NW_TENANT=alice make preflight` reaches the gateway with the tenant
   key and `make describe-azure` prints the tenant's names.
2. Project 1: `platform.pipelines.submit(tenant, "triage", ...)` runs the Azure ML pipeline as
   `northwind-alice-id`; the last step registers `northwind-alice-triage` version N as a
   candidate; the gate approves it; `platform.endpoints.deploy(tenant, version)` puts it on
   `nw-alice-triage-<scope>` and `/drift` on the service closes the loop. The weekly schedule
   runs `python -m nw.pipelines.retrain --pipeline triage --tenant alice --trigger schedule`
   on the cluster, and the gate decides, never the schedule.
3. Project 2: the same for `northwind-alice-semantic`.
4. Project 3: `platform.vectors.upsert(tenant, "policies", ...)` fills `northwind-alice-policies`
   (hybrid search, vectorized by the `text-embedding-3-small` deployment as the search service's
   identity). Every model call carries the `northwind-shields` content filter: Prompt Shields
   blocks user prompt attacks and document attacks. To screen before any model call, as
   Guardrails and Model Armor do on the other tracks, set `NW_AZURE_CONTENT_SAFETY_ENDPOINT` to
   the `NW_AZURE_CONTENT_SAFETY_ENDPOINT` output (`nw.agent.screen.AzurePromptShields`,
   `text:shieldPrompt`, api-version 2024-09-01): a blocked question is refused with reason
   `screened` and never reaches a model. The caller needs Cognitive Services User on the Foundry
   resource, which this template does not grant (it would also open the models past the
   gateway), so the apps do not get the variable by default; the Foundry resource has local auth
   off, so `NW_AZURE_CONTENT_SAFETY_KEY` works only where it is turned on.
5. Project 4: the agent service runs as the `northwind-alice-agent` app with its tools from
   `northwind-alice-mcp`, which only apps of the environment can reach; `platform.agents.deploy`
   runs the same image as the hosted agent `northwind-alice-agent` in the Foundry project, and
   its card goes into the registry; Foundry evaluations and traces go to Application Insights
   through the project's connection. The services' own spans are meant to follow through
   `nw/telemetry.py` (the Azure Monitor exporter, chosen by `APPLICATIONINSIGHTS_CONNECTION_STRING`
   on the azure track); with the lock's OpenTelemetry 1.42.1 the exporter does not import, so the
   services log a warning and export nothing until the pin moves (see `platform-azure` in
   `pyproject.toml`).
6. Capstone: the promotion drill below.

Every model call goes through the gateway: API Management counts tokens per tenant subscription
(`llm-token-limit`, 20,000 a minute by default) and emits them to Application Insights by
subscription (`llm-emit-token-metric`), which the workbook's "Model tokens per tenant" tile reads:
that is the per-tenant cost line.

## The promotion drill inside one resource group

Two artifacts promote, each with a canary, a human and the alerts that judge it:

**A model version into the live endpoint.** `platform.endpoints.deploy(tenant, version,
live=True, canary_percent=10)` creates the colour that is not serving (`blue` or `green`) on
`nw-live-triage-<scope>` and gives it 10 percent of the traffic. The alerts
`northwind-live-5xx` (RequestsPerMinute, statusCodeClass 5xx) and `northwind-live-p95`
(RequestLatency_P95 above 2,000 ms) watch the endpoint; the workbook shows both. Then
`promote` moves the rest and deletes the old colour, or `rollback` sends everything back.
Every live deployment carries data collection (`data_collector` on the ManagedOnlineDeployment
that `nw/platform/azure.py` creates). `request` and `response` payload logging fills without
code in the custom container, so every request and response the promoted version served is kept;
the data lands in `workspaceblobstore` under `modelDataCollector/<endpoint>/<deployment>/`,
partitioned by hour. `model_inputs` and `model_outputs`, the collections model monitoring reads,
are enabled but stay empty until the serving image logs through the `azureml-ai-monitoring`
`Collector`, which it does not yet, so an Azure ML model monitor has nothing to compute on; the
services' own `/drift` is the drift signal. Tenants opt in with `NW_AZURE_TENANT_DATA_COLLECTION=1`. In a cohort the instructor calls turns: the
live endpoint is shared, every tenant holds the endpoint role on it.

**A service image into the live apps.** `make release-azure` resolves the images tagged with the
current commit to digests, pins the serving revision of `northwind-live-policy` and
`northwind-live-agent`, adds a revision with the new digest and splits the traffic 90 to 10
(label `canary`). `make approve-azure REASON="..."` reads the fired alerts at resource group
scope (the deployer's role reaches no further) and refuses while a live alert fires or when the
alerts cannot be read, then gives the canary 100 percent and tags the app with the time and the
reason. `NW_FORCE=1` overrides both refusals. Then register the release:
`NW_TENANT=live make agent-cards PUSH=1`. On a push to `main` the
Azure Pipelines delivery pipeline does the same with an approval check between the two.

A redeploy of the template keeps what the drills set: `deploy_azure.sh` reads the endpoints'
traffic and the live apps' images and traffic before the deployment and passes them back.

## Lower and higher environments

The course builds one environment. An organisation deploys this template once per environment
and changes four things, all present already:

- **Environment name and target.** `NW_ENVIRONMENT=nwprod NW_AZURE_RESOURCE_GROUP=rg-nwprod`
  in a second resource group, or in a second subscription (`az account set`), is the higher
  environment. Its tenant list is empty (`NW_MODE=cohort NW_TENANTS=`, so `live` is the only
  owner): no learner works there, only the pipeline.
- **Second target of the same pipeline.** `azure-pipelines.yml` gains a stage per higher
  environment after `Promote`, with its own Azure DevOps environment and Approvals check and its
  own service connection bound to that environment's `nwprod-deployer`, whose federated
  credential trusts the same pipeline. The stage runs `deploy_azure.sh release` and `approve`
  with the higher environment's names; nothing in the lower environment can write to the higher
  one, the pipeline is the only path.
- **Images by digest.** The higher environment imports the digest the lower one approved
  (`az acr import --source <lower>.azurecr.io/nw-policy@sha256:...`, or a registry with a
  cache rule on the lower one); nothing is rebuilt.
- **Models across environments.** Azure ML workspaces are per environment. The promotion step
  shares the approved version through an Azure ML registry (`az ml model share`, or a registry
  both workspaces read) and deploys it on the higher environment's live endpoint with the same
  blue and green drill; the higher environment keeps its own alerts and data collection.

The shared services (the model gateway, observability, the registry) move to a shared resource
group or subscription as an organisation grows: API Management fronts both environments with a
product per environment, and the Log Analytics workspace takes both.

## Delivery

Azure Pipelines (`pipelines/azure-pipelines.yml`), set up once in the Azure DevOps project:

1. A service connection `northwind-deployer`: Azure Resource Manager, workload identity
   federation (manual), with the managed identity `northwind-deployer` (client id in
   `NW_AZURE_DEPLOYER_CLIENT_ID`). Redeploy with `NW_ADO_ISSUER` and `NW_ADO_SUBJECT` set to the
   issuer and subject the connection shows, so the identity trusts it.
2. Environments `northwind-canary` and `northwind-live`; on `northwind-live`, Approvals and
   checks, Approvals, the approvers. Approvals are not YAML: the environment's owner sets them.
3. A pipeline from the YAML on the GitHub repository.

GitHub Actions instead: `pipelines/github-actions.yml` (copy to `.github/workflows/`), the
environment `northwind-live` with required reviewers, `NW_GITHUB_REPOSITORY=owner/name` at
deploy so the deployer trusts `repo:owner/name:environment:northwind-live` and `ref:refs/heads/main`.
Pull-request checks stay in the existing Actions workflows (ADR 0012).

## Identity and security notes

- Every workload identity is a user-assigned managed identity; no client secret or storage key
  exists. Key Vault uses RBAC and each identity reads only its two secrets (role assignments on
  the secrets). The Foundry resource and AI Search have local auth off.
- A tenant writes blobs only under its prefix (and reads the shared `agents/` registry document):
  the Storage Blob Data Contributor assignments carry an ABAC condition on the blob path.
- Azure ML's finest scope for jobs and models is the workspace, so tenants share it under the
  custom role `northwind tenant on the workspace` (AzureML Data Scientist without workspace,
  compute, datastore, connection, schedule or endpoint writes) and operate only their own
  endpoints (the endpoint role, assigned on each endpoint) and the live ones.
- Defender for Containers scans every pushed image when `NW_DEFENDER=true`; it is a subscription
  plan, so the course leaves it to the organisation. Purview is the same (`NW_PURVIEW=true`).
- The apps' ingress is public with the cohort `x-api-key` (`nw/auth.py`); the MCP server has
  internal ingress only. An organisation adds Entra ID authentication on the Container Apps
  (built-in auth) and private endpoints.

## Costs, stop and destroy

Read the Azure track of `deploy/COSTS-platform.md` first. The meter while idle is API
Management v2 and AI Search (hourly, cannot be paused), the registry (daily) and whatever online
deployment holds a VM. `make stop-azure` deletes every online deployment (an approval brings it
back), keeps the apps at zero replicas when idle and stops the LiteLLM database; API Management
and Search bill until `make destroy-azure`, which deletes the resource group, purges the
soft-deleted Key Vault, Foundry resource and API Management names, removes the custom roles and
turns Defender back to Free when it was on. Nothing is billed afterwards.

## Scripts and where the API is

| Script | Called by | API |
| --- | --- | --- |
| `scripts/deploy_azure.sh deploy` | `make deploy-azure` | `az deployment group create`, `az storage blob upload --auth-mode login`, `az rest` to the AI Search REST API (indexes), `az role assignment create` |
| `scripts/deploy_azure.sh release`, `approve` | `make release-azure`, `approve-azure`, the delivery pipelines | `az containerapp update`, `az containerapp ingress traffic set`, Alerts Management REST |
| `scripts/deploy_azure.sh keys` | `make keys-azure` (LiteLLM only) | LiteLLM `POST /key/generate`, `/key/update` |
| `scripts/images_azure.sh` | `make images-azure`, the delivery pipelines | `docker buildx build --push`, `az acr` |
| `deploy/azure/scripts/azure_params.py` | `deploy_azure.sh` | none: parameters in, outputs out |

## Reviewing before a deploy

`tests/platform/test_azure_bicep.py` builds and lints `main.bicep`, checks every key of the
`outputs.json` contract is an output, and evaluates the compiled template for a cohort fixture
(`alice`, `bob`, APIM) and a solo fixture (LiteLLM): every tenant-scoped name carries the tenant,
each tenant gets its identity, apps, endpoints, schedule (disabled), gateway subscription, key
and prefix-bound blob roles; solo yields `solo` and `live` only; the model deployments, the AI
gateway policies and the pools are there once. `make what-if-azure` is the review against the
subscription.
