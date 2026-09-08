# Northwind AI Engineering

The participant repository for the AI Engineering course. One domain, support-ticket intelligence for Northwind Cloud, across six sessions.

## Language

### Course

**Session**: One 2-hour block, numbered 1 to 6. _Avoid_: Module, Lab, Day

**Step**: One numbered unit inside a session: an action and a check. _Avoid_: Task, Exercise

**Project**: A shipped service, numbered 1 to 4. Session 1 builds the shared service layer; Session 6 builds the Capstone. _Avoid_: Deliverable, Assignment

**Track**: A participant's cloud, AWS or GCP, chosen once. _Avoid_: Platform, Version

### Domain

**Northwind Cloud**: The fictional B2B SaaS company whose support desk everything serves.

**Ticket**: One support request: subject, body, type, queue, priority, tags, reference answer. _Avoid_: Case, Issue, Email

**Priority**: Northwind's urgency label on a ticket, P0 (outage or security) to P3 (question). _Avoid_: Severity, Urgency (in code)

**Queue**: The team a ticket is routed to. _Avoid_: Department, Team

**Policy corpus**: Northwind's policy documents that Project 3 retrieves from. _Avoid_: Knowledge base, Docs

**Golden set**: Fixed labelled evaluation cases, including unanswerable ones. _Avoid_: Test set (the classifier split), Benchmark

### Service layer

**Provider**: One implementation of the model interface for one vendor endpoint (Bedrock, Vertex, fake). _Avoid_: Backend, Client

**Model role**: A named slot (Workhorse, Judge, Economy) mapped to a model ID by configuration. Code names roles, never IDs. _Avoid_: Tier

**Completion**: One normalised model reply: text, tool calls, usage, stop reason. _Avoid_: Response, Generation

**Retryable error / Terminal error**: The two families every provider failure is mapped onto. _Avoid_: Transient, Fatal

**Spend cap**: The hard USD limit the client refuses to exceed. _Avoid_: Budget (that is the cloud alarm)

**Correlation ID**: The identifier that follows one request through every log line and model call. _Avoid_: Trace ID (that is OpenTelemetry's), Request ID (that is one model call)

### Project 1, triage

**Artifact**: One trained model version on disk: `model.joblib`, `metadata.json` with checksum and metrics, under `artifacts/triage/<version>`. `latest` is a symlink. _Avoid_: Checkpoint (that is Session 3's mid-training save), Pickle

**Decision rule**: How probabilities become a priority: P0 when its calibrated probability clears the threshold, otherwise the most likely class. _Avoid_: Argmax (that is one of the two branches), Cutoff

**Readiness**: The service can score a ticket right now. Distinct from liveness, which only says the process is up. _Avoid_: Health (ambiguous)


### Project 3, policy service

**Chunk**: One heading's worth of a policy document, with its metadata: document, section, effective date, audience, and whether a newer version supersedes it. The unit of retrieval and of citation. _Avoid_: Passage (in code), Snippet, Segment

**Citation**: A chunk id the answer relied on. Valid only if that chunk was in the context the model saw; validated in code, never trusted. _Avoid_: Reference, Source

**Refusal**: The service's answer when retrieval is weak, the model says the context does not cover the question, or no valid citation survives. A first-class outcome, measured by the harness. _Avoid_: Fallback, Error

**Golden set**: The fixed labelled cases for the policy service: answerable with gold chunk ids, unanswerable that must refuse, superseded traps, and permission-sensitive pairs. _Avoid_: Test set, Benchmark

**Regression gate**: The harness comparison against `baseline.json` that fails CI when a metric drops beyond tolerance. Citation validity must be exactly 1. _Avoid_: Threshold check, Quality bar


### Project 4, agents

**Tool**: One callable the model may use, with a name, a description, a strict argument schema, and a flag saying whether it is irreversible. Projects 1 to 3 and the customer records are tools. _Avoid_: Function, Action, Capability

**Registry**: The set of tools an agent can see, and the place arguments are validated and errors become observations. _Avoid_: Toolbox, Catalog

**Observation**: What a tool call returned, success or error, fed back to the model as data. _Avoid_: Result, Output

**Trajectory**: The full record of one agent run: every step, tool call, observation, token count and cost, the final answer, and how it terminated. Saved as a trace file. _Avoid_: Transcript, Log, Session

**Proposed action**: An irreversible tool call the model asked for that was recorded, not executed, pending approval. _Avoid_: Pending call, Draft action

**Specialist**: An agent with a narrower prompt and a subset of the registry, behind its own `/run`. The orchestrator uses specialists as tools. _Avoid_: Sub-agent, Worker

**Orchestrator**: The agent whose tools are the specialists. _Avoid_: Supervisor, Router (that is Session 6's cheap-model-first routing), Planner

**Adversarial set**: The fifteen tickets with expectations written as data, including injection, that every agent implementation must pass. _Avoid_: Red team set, Attack suite

### Session 6, deployment

**Session path**: What each participant deploys: the four services on App Runner or Cloud Run with least-privilege identity, readiness, metrics, alarms, a budget. _Avoid_: Lite stack, Dev deploy

**Reference stack**: The instructor's deployment: the agent on the managed agent runtime, the tool registry behind a gateway with a policy, a native guardrail, a managed vector index. _Avoid_: Prod stack, Full stack

**Screener**: The native service that inspects customer text before the loop: Bedrock Guardrails on AWS, Model Armor on GCP. _Avoid_: Filter, Firewall

**Router**: The capstone's cheap-model-first policy: Project 1 first, then the cheapest loop that will do. _Avoid_: Orchestrator (that is Session 5's agent), Dispatcher

**Cost sheet**: `deploy/COSTS.md`: rates, assumptions, idle cost, what stop and destroy leave behind, credit per participant. _Avoid_: Pricing, Estimate
