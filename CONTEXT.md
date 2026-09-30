# Northwind AI Engineering

The participant repository for the AI Engineering course: one domain, support-ticket intelligence for Northwind Cloud, from the first model call to a governed agent in production.

## Language

### Course

**Part**: One unit of the course, named by its title: Foundations and the AI service layer, Machine learning in production, Deep learning and representation, Generative AI: retrieval and evaluation, Agentic AI systems, Deployment, observability and capstone. Code identifiers keep a number (`session01`, `tests/session05`, `solutions/session-03`). _Avoid_: Module, Lab, Day

**Pre-work**: The self-paced part before the first: platform access on your track, the Local stack or your tenant, the first pipeline run. _Avoid_: Onboarding, Setup session

**Step**: One numbered unit inside a part: an action and a check that it worked. _Avoid_: Task, Exercise

**Project**: A shipped service, numbered 1 to 4. The first part builds the shared service layer, which is not a project; the last builds the Capstone, the integration of Projects 1 to 4. _Avoid_: Deliverable, Assignment

**Track**: Where you run the course, chosen once and never mixed: AWS, Google Cloud, Azure or Local (`NW_TRACK=aws|gcp|azure|local`). Local is the open-source stack on your own machine and needs no cloud account. _Avoid_: Version, Path, Cloud (Local is a track too)

**Mode**: How a cloud track is provisioned. Cohort: the instructor owns the platform and you are a tenant on it. Solo: you own the whole platform in your own account, at your own cost. _Avoid_: Tier

**Tenant**: Your namespace on a cohort platform (`NW_TENANT`): your pipelines, registered models, prompts, agent, ops records and cost line, isolated from every other participant's. _Avoid_: User, Workspace

**Platform**: The managed services a track runs on: pipelines, registries, endpoints, the model gateway, the retriever, the agent runtime, observability and delivery, in one environment. The contract is `nw/platform/base.py`. _Avoid_: Infra

**Environment**: One account, project or subscription holding a whole platform. The course builds one per track and shows how the same pipeline promotes into lower and higher environments. _Avoid_: Stage (a step inside a delivery pipeline)

### Domain

**Northwind Cloud**: The fictional B2B SaaS company whose support desk everything serves.

**Ticket**: One support request: subject, body, type, queue, priority, tags, reference answer. _Avoid_: Case, Issue, Email

**Priority**: Northwind's urgency label on a ticket, P0 (outage or security) to P3 (question). _Avoid_: Severity, Urgency (in code)

**Queue**: The team a ticket is routed to. _Avoid_: Department, Team

**Policy corpus**: Northwind's policy documents that Project 3 retrieves from. Mixed vintage on purpose. _Avoid_: Knowledge base, Docs

**Golden set**: Fixed labelled evaluation cases, including unanswerable ones. _Avoid_: Test set (the classifier split), Benchmark

**PII overlay**: `data/pii/`, fictitious personal data with labelled spans, used to measure redaction recall and precision. Nothing in it is real. _Avoid_: Test PII, Fake data

### Service layer

**Provider**: One implementation of the model interface for one vendor endpoint (Bedrock, the Agent Platform, Foundry, Ollama, fake). _Avoid_: Backend, Client

**Model role**: A named slot (Workhorse, Judge, Economy) mapped to a model ID by configuration. Code names roles, never IDs. _Avoid_: Tier

**Workhorse**: The role for production calls: gpt-oss, the same open-weight family on every track.

**Judge**: The role that scores outputs: a different, stronger model (Claude Opus 5), calibrated against human labels.

**Economy**: The cheapest role, for work that does not need the Workhorse.

**Model gateway**: The one door every model call goes through on a platform (LiteLLM, or API Management on Azure): it holds your key and budget, routes to the provider and attributes cost. The client in front of it keeps roles, the breaker and fallback. _Avoid_: Proxy, API gateway (the ingress in front of services)

**Completion**: One normalised model reply: text, tool calls, usage, cost, stop reason. _Avoid_: Response, Generation

**Retryable error / Terminal error**: The two families every provider failure is mapped onto. _Avoid_: Transient, Fatal

**Spend cap**: The hard USD limit the client refuses to exceed. _Avoid_: Budget (the cloud alarm)

**Residency**: The rule that an EU account's calls stay on EU models; a track with no EU model for a role refuses the call rather than leaving the region. _Avoid_: Data location

**Correlation ID**: The identifier that follows one request through every log line and model call. _Avoid_: Trace ID (OpenTelemetry's), Request ID (one model call)

### Project 1, triage

**Artifact**: One trained model version: `model.joblib`, `metadata.json` with checksum and metrics, a model card, under `artifacts/triage/<version>`. `latest` points at the promoted one. _Avoid_: Checkpoint, Pickle

**Candidate**: A trained version that has not passed the promotion gate. _Avoid_: New model, Release

**Promotion gate**: The comparison that decides whether a candidate replaces the live version. A failed gate leaves `latest` untouched. _Avoid_: Threshold check

**Decision rule**: How probabilities become a priority: P0 when its calibrated probability clears the threshold, otherwise the most likely class. _Avoid_: Argmax, Cutoff

**Shadow model**: A second version scored on every request and never served, so its agreement with the live one can be measured. _Avoid_: Canary (that serves real traffic)

**Drift**: A change in what the service sees compared with the training profile, measured as PSI and reported on `/drift`. An alert notifies; it never retrains or promotes by itself. _Avoid_: Decay

**Readiness**: The service can score a ticket right now. Distinct from liveness, which only says the process is up. _Avoid_: Health

### Project 3, policy service

**Retriever**: The one interface the policy service searches through: the track's managed retrieval service in the cloud, Qdrant on Local, in-process on a laptop. _Avoid_: Vector store, Index (the artifact it reads)

**Chunk**: One heading's worth of a policy document, with its metadata: document, section, effective date, audience, and whether a newer version supersedes it. The unit of retrieval and of citation. _Avoid_: Passage, Snippet

**Audience**: Who a chunk is for, customer or internal. Your API key decides yours, never the request body. _Avoid_: Role, Permission

**Citation**: A chunk id the answer relied on. Valid only if that chunk was in the context the model saw; validated in code, never trusted. _Avoid_: Reference, Source

**Refusal**: The answer when retrieval is weak, the model says the context does not cover the question, or no valid citation survives. A first-class outcome, measured. _Avoid_: Fallback, Error

**Redaction**: Replacing personal data with typed placeholders before text is stored, logged or indexed. _Avoid_: Masking, Anonymisation (a stronger claim)

**Regression gate**: The harness comparison against the baseline that fails CI when a metric drops beyond tolerance. Citation validity must be exactly 1. _Avoid_: Quality bar

### Project 4, agents

**Tool**: One callable the model may use, with a name, a description, a strict argument schema, and a flag saying whether it is irreversible. Projects 1 to 3 and the customer records are tools. _Avoid_: Function, Action

**Tool registry**: The set of tools an agent can see, and the place arguments are validated, the account is bound and errors become observations. _Avoid_: Toolbox, Catalog (the use case catalog)

**Observation**: What a tool call returned, success or error, fed back to the model as data. _Avoid_: Result, Output

**Trajectory**: The full record of one agent run: every step, tool call, observation, token count and cost, the final answer, and how it terminated. _Avoid_: Transcript, Log

**Proposed action**: An irreversible tool call the model asked for that was recorded, not executed, pending approval. _Avoid_: Pending call, Draft

**Approval**: A person, identified by the platform, executing a proposed action exactly as recorded, once. Nobody approves their own proposal. _Avoid_: Sign-off, Resume

**Ops store**: Where trajectories, proposals, approvals, escalations and feedback live: files under `artifacts/` on a laptop, your tenant's prefix in object storage on a platform (`NW_OPS_STORE`). _Avoid_: Trace dir, Database

**Specialist**: An agent with a narrower prompt and a subset of the tool registry, behind its own `/run`. The orchestrator uses specialists as tools. _Avoid_: Sub-agent, Worker

**Orchestrator**: The agent whose tools are the specialists. _Avoid_: Supervisor, Router (the capstone's routing), Planner

**Adversarial set**: The seventeen tickets with expectations written as data, including injection, cross-account access and similar-ticket leakage, that every agent implementation must pass. _Avoid_: Red team set

**Use case catalog**: The registered list of what the agents are for, each with its owner, risk class and approval state, registered before anything is built. _Avoid_: Backlog

**Agent registry**: The catalog of approved agents, tools and skills with their versions and owners. An agent that is not in it is not in production. _Avoid_: Inventory

**Evaluation tier**: What an evaluation measures: tool (one call), turn (one exchange), session (one run) or system (the business outcome). Every gate names its tier. _Avoid_: Level

**Screener**: The native service that inspects text before the loop and after tools: Bedrock Guardrails on AWS, Model Armor on Google Cloud, Prompt Shields on Azure, Llama Guard on Local. _Avoid_: Filter, Firewall

**Kill switch**: `NW_AGENT_DISABLED=1`: the agent answers 503 and makes no model call. _Avoid_: Feature flag

### Delivery and the capstone

**Promotion**: Moving an approved artifact (model, prompt, agent) from your tenant to the platform's live target through the delivery pipeline, with a human approval and a canary. The same step carries it into a higher environment when an organisation has several. _Avoid_: Deploy to prod, Release (the tagged version)

**Canary**: A slice of live traffic on the new version, widened only while its alarms stay quiet and rolled back when they fire. _Avoid_: Shadow (serves nobody)

**Router**: The capstone's cheap-model-first policy: Project 1 first, then the cheapest loop that will do. _Avoid_: Orchestrator, Dispatcher

**Cost sheet**: `deploy/COSTS-platform.md`: rates, assumptions, idle cost, what stop and destroy leave behind, credit per participant, per track and mode. _Avoid_: Pricing, Estimate
