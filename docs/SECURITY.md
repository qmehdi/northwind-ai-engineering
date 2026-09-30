# Security

What the Northwind system protects, the control for each threat, the file that implements it,
and what the control does not do. It covers the four tracks (`NW_TRACK=aws|gcp|azure|local`)
and the two modes (cohort, where the instructor owns the platform and learners are tenants;
solo, where one learner owns everything). Read it before you change a tool, a prompt, a capture
file, a workflow or a deployment. Checked against the code on 2026-09-30.

Governance lives beside it: the [DPIA](governance/dpia.md), the
[risk register](governance/risk-register.md) mapped to NIST AI 600-1, the
[datasheets](governance/datasheets.md), the
[retention and erasure runbook](governance/retention-and-erasure.md) and the
[residency and subprocessor table](governance/residency-and-subprocessors.md).

## The system and its trust boundaries

Four services (triage, semantic, policy, agent) plus an MCP server, each a container with the
same middleware, on one platform per track: SageMaker, Bedrock, AgentCore and CodePipeline on
AWS (`deploy/aws`); the Agent Platform (formerly Vertex AI), Cloud Run and Cloud Deploy on Google
Cloud (`deploy/gcp`); Azure Machine Learning, Microsoft Foundry, Container Apps and API
Management on Azure (`deploy/azure`); Docker Compose on Local (`deploy/local`,
`docker-compose.yml`). Every model call can go through the track's model gateway
(`NW_GATEWAY_URL`), which holds a key and a budget per tenant.

- Untrusted: ticket text, tool results that carry customer text, retrieved passages, model output
  until validated.
- Identity: an API key id per caller (`NW_API_KEY` as a JSON map of key id to secret). A key id
  is a caller, not a person.
- Tenant boundary (cohort mode): name prefixes, per-tenant identities and per-tenant gateway keys;
  its limits per cloud are below.

## Deliberate simplifications

These are choices for a course platform, stated so nobody mistakes them for production defaults.

- **Public endpoints behind keys.** Services, the gateway and the managed endpoints are reachable
  from the internet and protected by keys, IAM and rate limits, not by private networking. No WAF
  is deployed; the network edge is priced, not built (`deploy/COSTS-platform.md`).
- **One environment per cloud track.** The course builds one account or project and describes
  how the same pipeline promotes into lower and higher environments (workspace ADR 0008).
- **Local has no perimeter.** The compose stack is for one laptop; its services have no
  authentication of their own and ship example credentials.

## Threats and controls

| Threat | Control | Where | Limits |
| --- | --- | --- | --- |
| Unauthenticated use of a service | `x-api-key` checked in constant time against every key id; 401 on a wrong key, failed checks rate limited per client; without a key the service fails closed (503) except on Local or with `NW_AUTH_DISABLED=1`; probes and `/metrics` open | `nw/auth.py`, `nw/api.py` | `/metrics` is unauthenticated on every track. The agent runtime contract routes trust the platform's IAM when `NW_RUNTIME_AUTH=platform` (`nw/agent/agentcore.py`). The Agent Platform serving routes are opened when `AIP_HTTP_PORT` is set (`nw/serving/vertex.py`) and rely on the endpoint's IAM. The SageMaker handler relies on SageMaker IAM (`nw/serving/sagemaker/inference.py`) |
| A caller reads internal policy documents | The audience is the caller's, from its key id (`NW_POLICY_AUDIENCES`); unknown keys are customers; a request body can only narrow it; retrieval drops internal chunks for customers | `nw/policy/service.py` (`audience_of`), `nw/policy/retrieval.py`, `nw/platform/retrievers.py` | Internal and customer chunks share one index and are filtered after retrieval |
| Cost denial of service | Token bucket per key id with 429 and `Retry-After`; spend cap per process checked before every call; per-run budget and token cap; concurrency cap and kill switch (`NW_AGENT_DISABLED`) on the agent; circuit breaker per model; per-tenant gateway budgets; cloud budgets with alerts | `nw/ratelimit.py`, `nw/llm/cost.py`, `nw/agent/runcost.py`, `nw/agent/service.py`, `nw/llm/breaker.py`, `deploy/aws/scripts/gateway_keys.sh`, `deploy/gcp/modules/gateway`, `deploy/azure/modules/gateway.bicep`, `deploy/local/litellm/tenants.tsv` | Limiter, spend cap and breaker are per process and reset on restart. Cloud budgets alert; they do not stop spend. Azure API Management caps tokens per minute, not dollars |
| Prompt injection in a ticket | Task wrapped as untrusted data with the closing tag defused; system rules say it is data; input screening when configured (Bedrock Guardrails, Model Armor, Azure Prompt Shields; Llama Guard on the Local gateway); step, spend and token caps; irreversible tools are proposals until a person approves; 15 adversarial cases in the agent gate | `nw/agent/loop.py`, `nw/agent/tools.py` (`untrusted`), `nw/agent/screen.py`, `deploy/local/litellm/llama_guard.py`, `data/adversarial/tickets.jsonl`, `nw/agent/evaluate.py`, `.github/workflows/agent-gate.yml` | The default screener allows everything; a track without a guardrail id relies on the wrapper and the approval gate. The final reply is redacted, not screened. The ADK and Strands ports (`nw/agent/ports/`) do not redact or screen |
| Prompt injection through tool results | Successful tool output is redacted, screened when a screener is configured, wrapped as untrusted and capped at 8,000 characters; the approval gate is enforced in the tool registry and again by the platform (a Cedar policy on the AgentCore gateway forbids `escalate` to everyone but the approvers group) | `nw/agent/loop.py`, `nw/agent/tools.py`, `deploy/aws/stacks/areas/agents.py`, `docs/adr/0005-approval-gate-enforced-twice.md` | The platform-side enforcement exists on AWS only |
| One customer's data reaching another (confused deputy) | Agent tools that read an account are bound to the run's account and refuse any other; similar tickets are anonymised (full redaction, company names removed, ids dropped) before the model sees them | `nw/agent/northwind.py` (`bind_account`, `authorise_account`), `nw/semantic/embed.py` (`anonymise`) | With nothing bound, the lookup tools answer for any account: the MCP server, the framework ports and notebooks run unbound. The semantic service's `/classify` returns similar tickets to its own caller |
| An irreversible action without a person | `escalate` needs an approval that executes exactly the recorded arguments, once (a claim marker); approvers are recorded by platform identity | `nw/agent/approve.py`, `nw/agent/tools.py`, `docs/adr/0005-approval-gate-enforced-twice.md` | The approval CLI has no authorisation of its own: whoever can write the ops store can approve. Requester (a key id) and approver (a platform identity) are different namespaces, so self-approval is only partly detected |
| Personal data in prompts, logs, captures, traces and indexes | Redaction before a prompt, a cache, a log line, a capture line, a trajectory or an index (table below); logs carry ids, counts and versions, never text; spans carry no prompt text; gateway message logging off on the cloud gateways | `nw/policy/redact.py`, `nw/agent/trace.py`, `nw/logging.py`, `nw/telemetry.py`, `deploy/aws/stacks/areas/gateway.py`, `deploy/gcp/modules/gateway/main.tf`, `deploy/azure/modules/gateway.bicep` | See "What is and is not redacted" |
| EU personal data processed outside the EU | Residency routing: an EU account's calls use the EU model ids where one exists and fail rather than fall back to a non-EU model | `nw/llm/residency.py`, `nw/config.py` (`EU_MODELS`), `nw/llm/client.py` | The default deployments are in US regions; see the [residency table](governance/residency-and-subprocessors.md) for what each vendor offers |
| Data exfiltration through the agent | Account-bound tools refuse any other account; similar tickets are anonymised; tool output is redacted and wrapped as untrusted before the model reads it; the final reply is redacted in code whatever the model wrote; the agent's outbound network is an allow-list on AWS (VPC mode with a DNS firewall) and Azure (NSG egress) | `nw/agent/northwind.py`, `nw/agent/loop.py`, `nw/policy/redact.py`, `deploy/aws/stacks/areas/agents.py`, `deploy/azure` | Google Cloud's Agent Runtime has no egress allow-list in the course; the Local stack has none |
| Unsafe or ungrounded output harms | Answers cite only chunks the model saw, validated in code, and refuse when retrieval is weak or no citation survives; input screening (Bedrock Guardrails, Model Armor, Prompt Shields, Llama Guard); a calibrated Judge scores the gate and a sample of live runs, and a quality alert fires below its bar | `nw/policy/answer.py`, `nw/agent/screen.py`, `nw/agent/monitor.py`, `nw/quality.py` | Screening is on the input path; a harmful but grounded answer is caught by evaluation and review, not blocked inline |
| Secret leakage | Keys from the environment or the cloud secret store by reference, never in images; constant-time comparison; key ids, not secrets, in logs and metrics; gitleaks in pre-commit and over history in CI | `nw/auth.py`, `nw/serving/gateway.py`, `.pre-commit-config.yaml`, `.github/workflows/audit.yml`, `scripts/rotate_key.sh` | Google Cloud secret values are generated by Terraform and live in its state. `scripts/rotate_key.sh` covers AWS and Google Cloud only |
| Supply chain: code and dependencies | `uv.lock` installed with `--locked`; pip-audit over the lock weekly and on every pull request; Python base images pinned by digest | `uv.lock`, `Dockerfile`, `.github/workflows/audit.yml` | Hugging Face models load from the default branch until the revision map (`HF_REVISIONS` in `nw/config.py`) is used by every loader. The triage model's hash sits next to the pickle, so it detects corruption, not tampering |
| Supply chain: CI and images | Every third-party GitHub Action pinned by commit SHA with its release in a comment; read-only `GITHUB_TOKEN` by default, elevated per job; image scanned by Trivy (CRITICAL and HIGH with a fix fail) with an SBOM; on main the pushed digest is signed with cosign keyless and the signature verified against the workflow identity; a release verifies each image's signature before tagging it | `.github/workflows/ci.yml`, `image.yml`, `audit.yml`, `release.yml` | CI builds and signs the triage image only. The images the cloud delivery pipelines build (CodeBuild, Cloud Build, `scripts/images_azure.sh`) are not signed or verified at deploy |
| Data or golden set changed silently | Every data file hashed in the manifest with a dataset version; CI fails on drift; the policy index carries the corpus hash and reports staleness | `data/MANIFEST.json`, `nw/data_manifest.py`, `nw/policy/manifest.py` | Nothing checks the manifest at runtime |
| An agent reaching data it is not approved for | Each agent's reachable data classes are derived from its tools and delegates and must be covered by its approved use case; every approved use case names its EU AI Act class, DPIA section and a privacy approver distinct from the business approver | `nw/agent/catalog.py` (`--check`), `data/use_cases.yaml`, `nw/agent/registry.py`, `data/agents/` | Output classes are declared per tool in the catalog; a tool author can still return more than declared |
| Breaking an API client silently | Routes versioned under `/v1`; OpenAPI snapshots checked on every pull request | `nw/api.py`, `docs/openapi/`, `scripts/openapi_snapshot.py` | |

## What is and is not redacted

Redaction replaces a match with a token like `[EMAIL_1]`, stable within one text. Pattern kinds:
EMAIL, IBAN, INVOICE, ACCOUNT, CARD, PHONE, IP, KEY (`nw/policy/redact.py`). Names and addresses
need a detector: `NW_REDACT_DETECTOR=heuristic` or `presidio`. The default is patterns only.
Measure any configuration with `uv run python -m nw.policy.redact --eval data/pii/messages.jsonl`
(the fictitious PII overlay; baseline in `data/pii/README.md`).

| Path | Redacted | Not redacted |
| --- | --- | --- |
| Policy `/ask` question | Before screening, cache, model, capture and logs | Names and addresses without a detector |
| Agent task and tool observations | Before the screener and the model, keeping account and invoice ids so tools work | Names and addresses without a detector; the task on the ADK and Strands ports |
| Agent final reply | Full redaction | |
| Trajectories | Task, thoughts, arguments, observations, final reply (account and invoice ids kept) | |
| Escalation justification | Before it is queued | |
| Similar-ticket index | Anonymised when the index is built (`nw/semantic/embed.py`) | Ticket ids stay in `ids.json` |
| Triage, semantic, policy and agent captures, feedback | Free-text fields before the line is written | |
| Policy index | Chunks at build (`nw/policy/chunking.py`) | |
| Model training data and artifacts | Nothing: models train on `data/tickets.jsonl`, which holds no personal data by design | A real deployment's triage vocabulary could keep repeated personal tokens |
| Managed endpoint capture (SageMaker data capture, Agent Platform request logging, Azure ML data collector) | Nothing | Raw request and response payloads, kept 90 days (see the runbook) |

## Tenant isolation in cohort mode, per cloud

Each tenant gets its own prefixes, identities and gateway key. What a tenant can still reach:

| Track | Isolated | Shared or reachable across tenants |
| --- | --- | --- |
| AWS | SageMaker execution role per tenant with read-write on `tenants/<t>/*`; one vector index and knowledge base per owner; per-tenant gateway keys and application inference profiles | The MLflow tracking server and its artifacts; the Studio domain; one cohort service API key; one AgentCore runtime role for every tenant runtime; the knowledge base role (`deploy/aws/stacks/areas/tracking.py`, `agents.py`, `prompts.py`) |
| Google Cloud | Service accounts per tenant and service; per-tenant RAG corpus and gateway key | Project-level Agent Platform roles; bucket-wide object roles without prefix conditions; the Agent Engine service agent's secret access; one cohort service API key (`deploy/gcp/modules/tracking`, `agents`) |
| Azure | A managed identity per owner; blob ABAC on the tenant's path in the lake; per-tenant Key Vault secrets | The workspace storage account (holds the live data collector payloads); workspace job and model write; the live endpoints' operator role; the Foundry project; the Container Apps environment, so internal ingress (MCP) is reachable from other tenants' apps (`deploy/azure/modules/tenant.bicep`, `tracking.bicep`) |
| Local | Virtual keys and budgets per tenant in LiteLLM | Everything else: MLflow, Qdrant, Postgres and the registry have no authentication |

Solo mode has one tenant, so these limits do not apply; the learner's own account is the boundary.

## Known limits

- Services are public endpoints behind keys by design; a production edge (WAF, private ingress)
  is priced in the cost sheet and not deployed.
- A tenant's Foundry hosted agent on Azure runs as the project's shared identity, which holds no
  storage role, so its trajectories stay in the container until each agent has its own identity.
- No internal ops key is created on any track, so every policy caller is `customer`
  (`NW_POLICY_AUDIENCES` stays unset).

## Reporting

Report a vulnerability in this repository or in a deployed cohort platform by email to the
course author at the address on the GitHub profile of `techwithshadab`, with the subject
`northwind security`. Do not open a public issue. You will get an acknowledgement within three
working days and a fix or a mitigation plan within fourteen. Reports about a learner's own
account belong with that learner's cloud provider.

## Out of scope

- The hosted models and the providers' guardrail services (Bedrock, the Agent Platform, Foundry,
  Anthropic): report to the provider.
- The synthetic data: it describes no real person. The PII overlay in `data/pii/` is fictitious
  by construction.
- A learner's own account after the course: `make destroy-aws`, `make destroy-gcp` and
  `make destroy-azure` remove the platform.
- The Local compose stack on a shared network: it is built for one laptop.
