# Data protection impact assessment: Northwind support intelligence

A DPIA under GDPR Article 35, written as a reusable template and filled for the course system.
Northwind Cloud is fictitious and every record in `data/` is synthetic; the assessment is real
practice on a made-up controller. It is not legal advice.

| Field | Value |
| --- | --- |
| Controller | Northwind Cloud (fictitious), support operations |
| System | Support intelligence: triage (Project 1), semantic tagging (Project 2), policy answers (Project 3), agents (Project 4), the capstone router |
| Assessment owner | `privacy-office` (the privacy approver in `data/use_cases.yaml`) |
| Business approver | `support-operations` |
| Version and date | 1.0, 2026-09-30 |
| Next review | on any trigger in "Review triggers", and at least every 12 months |
| Related | [risk register](risk-register.md), [datasheets](datasheets.md), [retention and erasure](retention-and-erasure.md), [residency and subprocessors](residency-and-subprocessors.md), [SECURITY.md](../SECURITY.md) |

## How to use this template

1. Copy the file, keep the headings, replace every value.
2. One "Use case" section per entry in `data/use_cases.yaml`; its heading carries the use case
   id, because `python -m nw.agent.catalog --check` fails when an approved use case's `dpia_ref`
   does not resolve to a section here.
3. The data a use case reaches is derived by the catalog check from the tools, not written by
   hand: paste the `reachable_data_classes` line from the agent card (`data/agents/<name>.json`).
4. Every measure names the file that implements it. A measure with no file is a plan; list it
   under "Open actions" with an owner.

## 1. Why a DPIA is needed

Article 35(1) asks for one when processing "is likely to result in a high risk". Screening
against the EDPB criteria (WP248 rev.01):

| Criterion | Applies | Why |
| --- | --- | --- |
| Innovative technology | Yes | Language models and agents act on free text |
| Evaluation or scoring | Partly | Priority and tags score tickets, not people; no decision about a person follows |
| Automated decision with legal or similar effect | No | Escalations and refunds are proposed and approved by a person (`nw/agent/approve.py`) |
| Large scale | Yes, for a real desk | Every ticket passes through the models |
| Data transferred outside the EU | Yes, by default | See section 5 |
| Vulnerable data subjects, special categories | Not by design | Tickets can contain anything a customer types; see risk R4 |

Two criteria or more: a DPIA is required.

## 2. Description of the processing

**Nature.** Customers of business accounts write tickets. The system classifies priority
(Project 1, `nw/triage`), tags topics (Project 2, `nw/semantic`), answers policy questions
with citations (Project 3, `nw/policy`), and drafts replies or proposes escalations through
agents (Project 4, `nw/agent`). Model calls go through a model gateway on the cloud tracks.
Support agents read every draft before it is sent; escalations wait for a human approval.

**Scope.** Data subjects: contact persons at business customers, and anyone they name in a
ticket. Personal data: names, email addresses, phone numbers, postal addresses, bank details and
free text inside tickets; account ids link a ticket to a business, not directly to a person.
Volume in the course: 7,950 synthetic tickets, 240 accounts, and the 600-message fictitious PII
overlay in `data/pii/` for privacy drills.

**Context.** 67 of 240 accounts are `region: eu` and 388 tickets are in German. Northwind's own
customer policy (`data/policies/gdpr-data-residency.md`) promises EU processing for EU accounts
and 30-day erasure (`data/policies/data-retention.md`, section 6).

**Purposes.** Faster, consistent first response; correct routing of urgent tickets; answers that
match the current policy.

## 3. Data inventory

The data classes are the ones the catalog uses (`DATA_CLASS_INFO` in `nw/agent/catalog.py`).

| Data class | Personal data | Source | Where it goes |
| --- | --- | --- | --- |
| `ticket_text` | Yes | The customer | Models (after redaction), captures, trajectories |
| `customer_account` | No (business data) | `data/accounts.json` | Agent observations, trajectories |
| `entitlements` | No | Derived from the account | Agent observations |
| `policy_corpus` | No | `data/policies/` | Index, answers |
| `past_tickets` | Yes, other customers | The similar-ticket index | Agent observations, after anonymisation |
| `escalation_queue` | Yes, in justifications | Agents | Escalation store, approvals |

Every store, its retention and its erasure path: [retention and erasure](retention-and-erasure.md).

## 4. Necessity and proportionality

- **Lawful basis.** Article 6(1)(b) for answering the customer's own ticket; Article 6(1)(f)
  (legitimate interest in running a support desk) for triage, analytics and model improvement,
  with the balancing test recorded here. Not consent.
- **Minimisation.** Identifiers are tokenised before a prompt, a log, a cache or an index
  (`nw/policy/redact.py`). Agents keep account and invoice ids so tools work, and nothing else
  (`redact_for_agent`). Names and addresses are only caught with a detector switched on
  (`NW_REDACT_DETECTOR=heuristic|presidio`); the default does patterns only, a gap listed in R4.
- **Purpose limitation.** The PII overlay is never training data (`data/pii/README.md`).
  Captured traffic feeds backtests and drift, not retraining, until a person commits a row.
- **Accuracy.** Policy answers cite passages and refuse when retrieval is weak
  (`nw/policy/answer.py`); golden sets gate every release ([risk register](risk-register.md), R2).
- **Storage limitation.** 90 days for captures and traces, 400 days for audit records; the full
  table is in [retention and erasure](retention-and-erasure.md).
- **Rights.** Access and erasure run through the runbook; the pseudonymous `subject_key` in the
  overlay is the drill's handle.
- **Transfers.** Section 5.

## 5. International transfers and residency

The cloud tracks default to US regions (AWS `us-east-1`, Google Cloud `us-central1` with the
Judge in `us-east5`, Azure `eastus2` with GlobalStandard deployments). An EU account's ticket is
therefore processed in the US unless the platform is deployed in an EU region or the residency
routing sends it to EU model ids (`nw/llm/residency.py`, `EU_MODELS` in `nw/config.py`). Per
vendor facts, dates and the transfer mechanism: [residency and subprocessors](residency-and-subprocessors.md).

## 6. Risks to data subjects and measures

Likelihood and severity on a 1 to 3 scale for a real deployment; residual after the measures.

| Id | Risk to people | L | S | Measures (file) | Residual |
| --- | --- | :-: | :-: | --- | --- |
| D1 | Personal data in a prompt reaches a model provider | 3 | 2 | Redaction before the model on the policy and agent paths (`nw/policy/answer.py`, `nw/agent/loop.py`); gateway message logging off on the cloud gateways; providers do not train on API data (subprocessor table) | Medium: names and addresses pass unless a detector is on |
| D2 | One customer's data shown to another | 2 | 3 | Agent tools bound to the run's account (`bind_account` in `nw/agent/northwind.py`); similar tickets anonymised (`anonymise_similar`); tool output screened when a screener is configured (`nw/agent/screen.py`) | Medium: unbound callers (MCP server, framework ports) fail open (A3); the semantic service's `/classify` returns raw similar tickets to its caller |
| D3 | Internal policy text quoted to a customer | 2 | 2 | The audience is the caller's, from its key id (`NW_POLICY_AUDIENCES`, `audience_of` in `nw/policy/service.py`); unknown keys are customers; the retrieval filter drops internal chunks (`nw/policy/retrieval.py`) | Low: a request body can only narrow its audience |
| D4 | Data kept longer than needed, or not erasable in 30 days | 3 | 2 | Lifecycle rules in IaC; redaction at write; runbook | Medium: captures carry no account id, so erasure is by content scan |
| D5 | EU personal data processed outside the EU | 3 | 2 | EU model ids and routing (`nw/llm/residency.py`); EU region deployment | High on the default US deployments; see section 5 |
| D6 | A wrong automated action affects a person | 1 | 3 | Escalation and refund need a human approval bound to the recorded arguments (`nw/agent/approve.py`, Cedar policy on AgentCore) | Low |
| D7 | Tenant in a cohort reads another tenant's data | 2 | 2 | Per-tenant prefixes and identities in IaC | Medium: isolation limits per cloud are listed in SECURITY.md |
| D8 | Special category data typed into a ticket | 2 | 3 | Nothing detects it | High: A4 |

## 7. Use cases

Each section is the target of a `dpia_ref` in `data/use_cases.yaml`.

### uc-triage

- Purpose: customer, plan, priority and tags before a person reads the ticket.
- Reaches: `ticket_text`, `customer_account`, `entitlements`, `past_tickets` (the semantic
  service lists similar tickets on the HTTP backend).
- EU AI Act: minimal risk. It ranks tickets, not people, and no Annex III area applies (not
  employment, credit, essential public services or emergency dispatch). No output reaches a
  person outside Northwind, so no Article 50 disclosure.
- Specific risks: D2 through `past_tickets`.
- Sign-off: `privacy-office`, 2026-09-30.

### uc-policy

- Purpose: answer policy questions from the current corpus with citations, or refuse.
- Reaches: `ticket_text`, `policy_corpus`.
- EU AI Act: Article 50(1). The answer can reach a customer directly, so it carries the
  disclosure in the catalog.
- Specific risks: D3 (internal documents), D1.
- Sign-off: `privacy-office`, 2026-09-30.

### uc-resolution

- Purpose: draft the reply and propose an escalation when priority or policy requires one.
- Reaches: `ticket_text`, `past_tickets`, `escalation_queue`.
- EU AI Act: Article 50(1) disclosure on the drafted reply; a support agent checks each draft.
- Specific risks: D2 through similar tickets; D6 through escalation.
- Sign-off: `privacy-office`, 2026-09-30.

### uc-resolver

- Purpose: one agent resolves a ticket end to end with every tool; escalation is proposed only.
- Reaches: all six data classes.
- EU AI Act: Article 50(1) disclosure on the draft.
- Specific risks: D1, D2, D6.
- Sign-off: `privacy-office`, 2026-09-30.

### uc-orchestrator

- Purpose: combine the three specialists into one reply.
- Reaches: all six data classes, through the specialists' replies (derived, not declared).
- EU AI Act: Article 50(1) disclosure on the draft.
- Specific risks: D2 (specialists run on separate services; the account binding must travel
  with the request).
- Sign-off: `privacy-office`, 2026-09-30.

### uc-router

- Purpose: the cheapest path that resolves a ticket; a confident P0 goes to the duty manager
  without a model call.
- Reaches: all six data classes.
- EU AI Act: Article 50(1). The capstone can answer directly, so the disclosure offers a person.
- Specific risks: D1, D2, D5 (Economy model routing).
- Sign-off: `privacy-office`, 2026-09-30.

### uc-refund

- Purpose (draft, not built): issue refunds inside the policy limit without a person.
- Reaches (declared): `ticket_text`, `customer_account`, `policy_corpus`, `escalation_queue`.
- EU AI Act: Article 50(1) disclosure. Refunds to business customers are not an Annex III area;
  reassess if consumers are ever served, since money moving without a person is also
  Article 22 GDPR territory for natural persons.
- Status: not assessed for approval. Needs two approvers (risk class high) and this DPIA's
  sign-off before `approval: approved`.

## 8. Open actions

| Id | Action | Owner | Tracks |
| --- | --- | --- | --- |
| A1 | Residency routing wired end to end, EU deployment option documented per cloud | developer, platform engineer | cloud |
| A2 | Name and address detection on by default where a detector is available; recall measured on `data/pii/` | developer | all |
| A3 | Account binding required on every agent entry point (MCP server, framework ports) | developer | all |
| A4 | Special category detection (health, union membership and so on) or a documented refusal to store | privacy-office | all |
| A5 | An erasure tool that walks every store in the runbook by account id and subject key | data engineer | all |

## Review triggers

A new data class or tool, a new model provider or region, a new use case or a risk class
change, a redaction recall drop on `data/pii/` (test split), an incident involving personal
data, and 12 months since the last review.

## Sign-off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Privacy approver | privacy-office | Approved with the open actions above | 2026-09-30 |
| Business approver | support-operations | Accepted residual risks D2, D4, D7 | 2026-09-30 |
| Security | platform engineer | Reviewed section 6 | 2026-09-30 |
