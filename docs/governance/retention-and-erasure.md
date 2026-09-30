# Retention and erasure runbook

Every place the system keeps data, how long, why, who owns it, and how an erasure request
reaches it. Northwind promises its customers erasure within 30 days of a verified request
(`data/policies/data-retention.md`, section 6); this runbook is how the support intelligence
system keeps that promise, and where it cannot yet.

## Retention classes

| Class | TTL | What falls in it |
| --- | --- | --- |
| Operational | 90 days | Captures, agent trajectories, feedback, telemetry traces, model endpoint request logs, gateway spend logs, monitoring output |
| Audit | 400 days | Approvals, escalation records, cloud audit trails (CloudTrail, Cloud Audit Logs, Activity Log), storage access logs, model invocation metadata |
| Logs | 30 days | Application and runtime logs (no ticket text by design) |
| Agent memory | 30 days | AgentCore Memory events |
| Model lifetime | while the version is served, plus one superseded version | Model artifacts, indexes, run records, MLflow runs |
| Source | set by the customer's plan | Tickets and accounts in the ticketing system (here the synthetic `data/`) |

The IaC sets these TTLs as lifecycle rules and log retention; the owner of each track keeps
them in step with this table. Noncurrent object versions expire after 30 days so a deleted
object does not survive in a version.

## How an erasure request reaches each store

A request names a person. The runbook resolves the person to the handles the stores are keyed
by: the account id (`NW-10784`), ticket ids, the run ids of agent runs on those tickets, and the
person's direct identifiers (name, email, phone) for stores that can only be searched by
content. In the drill, `data/pii/subjects.jsonl` gives the person and `subject_keys` in
`data/pii/messages.jsonl` gives their messages.

"Ages out" means the TTL is at most 30 days, so no action is needed to meet the promise.
"Not personal" means the store holds no personal data by design; the check is to confirm it.

### Application stores (every track)

| Store | What | Why | TTL | Owner | Erasure path |
| --- | --- | --- | --- | --- | --- |
| Tickets (`data/tickets.jsonl`; the ticketing system in production) | Subject, body, answer, labels | Source of truth, training data | Customer plan | data engineer | Delete the rows. Then every derived store below: indexes rebuilt, models retrained |
| Triage and semantic models (`artifacts/triage/`, `artifacts/semantic/`) | Model weights; the triage TF-IDF vocabulary keeps n-grams seen at least twice | Serving | Model lifetime | data engineer | Retrain without the erased rows (the weekly `retrain-*.yml` candidate), promote, retire the old version from the registry within 30 days. A model is not searched for a person |
| Similar-ticket index (`artifacts/index/`: `ids.json`, `meta.jsonl`) | Ticket ids, subject, first 500 characters of the answer, labels; unredacted at build | `find_similar_tickets` | Model lifetime | data engineer | Rebuild the index without the erased tickets and redeploy the image or artifact that carries it |
| Policy index (`artifacts/policy/`) and the cloud retrievers (Bedrock KB on S3 Vectors, Agent Platform RAG, AI Search, Qdrant) | Policy chunks, redacted at build | Retrieval | Model lifetime | domain expert | Not personal |
| Captures (`NW_TRIAGE_CAPTURE`, `NW_SEMANTIC_CAPTURE`, `NW_POLICY_CAPTURE`, `NW_AGENT_CAPTURE`) | Redacted request text, predictions, versions; no account id | Backtest, shadow comparison, drift | 90 days | data engineer | Search by the person's name and address (identifiers are already tokens); delete matching lines. Without a name detector (`NW_REDACT_DETECTOR`) names survive redaction, so the search is required |
| Agent trajectories (`NW_TRACE_DIR`, or `traces/` in the ops store `NW_OPS_STORE`) | Redacted task, steps, observations, final reply; keeps `account_id` and invoice ids | Review, evaluation, approval | 90 days | platform engineer | Select by `account_id`; delete the runs of the erased tickets, and search the rest for the name |
| Approvals (`artifacts/approvals/`, or the ops store) | Run id, tool, recorded arguments, approver, requester | Accountability | 400 days | reviewer | Kept under Article 17(3)(b) and (e) (legal obligation, legal claims). Arguments are redacted; if a name appears, replace it with its token in place and log the change |
| Escalation queue (`NW_ESCALATION_QUEUE`, or `escalations/queue` in the ops store) | Ticket id, tier, redacted justification | Paging, dedupe, audit | 400 days | reviewer | As approvals |
| Policy feedback (`NW_POLICY_FEEDBACK`) | Verdict, redacted note, question, answer | Golden set candidates | 90 days | domain expert | Search by name; delete lines. A row already committed to `data/golden/` is removed by a reviewed commit |
| Response cache (`nw/policy/cache.py`) | Answers keyed by a hash of the redacted question | Latency, cost | `NW_POLICY_CACHE_TTL_S`, in memory | platform engineer | Ages out; a restart clears it |
| Run records (`runs.jsonl`), MLflow runs, model cards, data profiles | Params, metrics, hashes, aggregate profiles | Lineage | Model lifetime | data engineer | Not personal (data check findings can list ticket ids; delete those lines) |
| Application logs | Access lines (method, path, status, key id, duration), run and tool lines without text | Operations | 30 days | platform engineer | Ages out |
| Telemetry traces (spans) | Ids, model, tokens, cost, tool names; no prompt text | Debugging | 90 days | platform engineer | Not personal |

### Platform stores per track

| Store | AWS | Google Cloud | Azure | Local | TTL | Erasure path |
| --- | --- | --- | --- | --- | --- | --- |
| Endpoint request capture (payloads of the served classifiers) | SageMaker data capture to `artifacts/capture/` | Endpoint request and response logging to BigQuery `live_<model>_requests` | Azure ML data collector in `workspaceblobstore/modelDataCollector/` | Capture files | 90 days | Raw payloads: search by name, email and phone; delete the objects or rows. The largest erasure risk: payloads are not redacted by the platform |
| Agent memory | AgentCore Memory, per tenant | none | Foundry hosted agent threads | none | 30 days | Ages out on AWS; delete the thread on Azure when the person is identified |
| Model gateway spend logs | LiteLLM on Aurora | LiteLLM on Cloud SQL | API Management analytics, LiteLLM on Postgres when used | LiteLLM on Postgres | 90 days | Not personal: key id, model, tokens, cost. Message logging is off on the cloud gateways |
| Model invocation logs | Bedrock invocation logging, metadata only | Cloud Audit Logs for Vertex AI | Foundry diagnostic logs | none | 400 days | Not personal: no prompt text is delivered |
| Monitoring output | Model Monitor in `artifacts/monitoring/` | Model Monitoring jobs | Azure ML monitoring | Evidently reports | 90 days | Aggregate; not personal |
| Audit trail | CloudTrail with log file validation | Cloud Audit Logs | Activity Log | none | 400 days | Not personal |
| Object versions | S3 versioning | GCS versioning on `artifacts` | Blob soft delete | none | 30 days after deletion | Ages out after the delete above |
| Backups | Aurora automated backups | Cloud SQL backups | Postgres backups | none | 7 days | Ages out |
| Provider side | Bedrock stores no prompts | Agent Platform: see the subprocessor table | Foundry: see the subprocessor table | Anthropic API for the Judge: see the subprocessor table | per vendor | See [residency and subprocessors](residency-and-subprocessors.md) |

## Procedure

1. **Verify** the requester through the privacy contact channel; open a ticket for the request
   with a pseudonymous handle, never the person's details, in the title.
2. **Resolve** the handles: account id, ticket ids, run ids (trajectories carry the ticket id in
   the task and the `account_id` field), and the direct identifiers to search for.
3. **Find**, store by store, in the order of the tables above. On the Local track and in the
   drill:

   ```bash
   KEY=ds-04569ea88d4a                                   # from data/pii/subjects.jsonl
   jq -c --arg k "$KEY" 'select(.subject_keys | index($k))' data/pii/messages.jsonl
   jq -r --arg k "$KEY" 'select(.subject_key == $k) | .name, .email, .account_id' data/pii/subjects.jsonl
   rg -l -F -e 'NW-10784' artifacts/traces/                # trajectories by account
   rg -n -F -e 'Hana Quillbury' artifacts/ --glob '*.jsonl' # captures and feedback by name
   ```

   On a cloud track, run the same searches against the stores in the platform table (S3 Select
   or Athena over the capture prefix, a BigQuery query on the request log table, Azure Storage
   Explorer or `az storage blob` over the collector path).
4. **Delete or rebuild**: delete matching lines and objects; rebuild the similar-ticket index;
   schedule the retrain and the promotion; leave audit stores in place, tokenising any name.
5. **Record** the erasure in the request ticket: stores searched, counts deleted, the model
   version that will be retired and its date. No personal data in the record.
6. **Confirm** to the requester within 30 days, naming the backup and version windows that
   still hold copies until they age out.

## Known gaps

- Captures and endpoint request logs carry no account id or subject key, so erasure is by
  content search, and names survive redaction unless a name detector is on.
- There is no single erasure tool; the steps above are manual.
- A trained model cannot be searched for a person; erasure from a model means retraining and
  retiring the old version, which the retrain workflows prepare but never promote by themselves.
- The 90-day operational TTL is longer than the 30-day erasure promise, so targeted deletion,
  not the TTL, is what meets it for those stores.
