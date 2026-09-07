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
