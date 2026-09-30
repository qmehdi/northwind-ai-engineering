# Datasheets

One datasheet per dataset the course ships, after "Datasheets for Datasets" (Gebru et al.,
2021): motivation, composition, collection, preprocessing, uses, distribution, maintenance.
Counts are as of 2026-09-30; `data/MANIFEST.json` holds the current hash, size and row count of
every file and the dataset version (`make check-data` fails when a file moved without it).

## tickets.jsonl

| Question | Answer |
| --- | --- |
| Motivation | Train and evaluate the priority classifier (Project 1) and the topic tagger (Project 2), and give the agents (Project 4) realistic tickets. Built for the course; no funding beyond it |
| Composition | 7,950 support tickets for the fictitious Northwind Cloud: subject, body, reference answer, type, queue, priority P0 to P3, 3 to 7 tags, language (`en` 95.1 percent, `de` 4.9 percent), messiness flags, product, split. Schema and distributions in `data/README.md` |
| Personal data | None by design. The generator told the model to write no emails, phone numbers or surnames; the audit on 2026-09-29 counted 2 emails, 0 phone numbers and 0 names in 7,950 rows. For privacy lessons use the PII overlay below, never this file |
| Collection | Labels decided in code (`data-gen/northwind.py`) from distributions modelled on the public dataset Tobi-Bueck/customer-support-tickets (CC BY-NC 4.0, English subset, label and length statistics only); text written by a language model from those briefs (`data-gen/generate_tickets.py`). No row of the reference dataset is stored or copied (ADR 0003) |
| Preprocessing | Split 80/10/10 stratified by priority; 5 of 800 generation batches skipped by the count check; P0 carved out of "high" by the documented rule |
| Known limits | Cleaner than real tickets (10 percent carry a messiness flag); German is 388 rows, so German P0 metrics rest on few cases (use `data/golden/triage_slices.jsonl` for the German and P0 slices); 12 percent of queues deliberately misrouted; no multi-turn threads |
| Uses | Training, validation, test, drift baselines, agent tasks. Not for: benchmarking real support systems, privacy or redaction lessons, any claim about real customers |
| Distribution | Committed in the participant repo. Synthetic, for the course and the cohort's use; the reference dataset's non-commercial licence governs any reuse of its statistics |
| Maintenance | Regenerated only by `data-gen/generate_tickets.py`; a change bumps the dataset version in the manifest and requires re-measuring every number the guide quotes. Owner: data engineer |

## accounts.json

| Question | Answer |
| --- | --- |
| Composition | 240 fictitious business accounts: id, company, tier, region (`us` 55, `eu` 35, `apac` 10 percent by design; 67 are `eu`), seats, industry, features, SLA hours |
| Personal data | None: companies only, no people. Contact persons exist only in `data/pii/` |
| Uses | Agent tools `lookup_customer` and `check_entitlement`; residency routing keys on `region` |
| Maintenance | Seeded (`make_accounts(seed=7)` in `data-gen/northwind.py`); never edited by hand |

## Policy corpus (data/policies/)

| Question | Answer |
| --- | --- |
| Motivation | The documents the policy service (Project 3) retrieves from and cites |
| Composition | 28 markdown documents plus `index.json`: each has `doc_id`, `audience` (`customer` or `internal`), `effective`, `supersedes`. Four are internal (`account-verification-internal`, `escalation-matrix`, `refund-approval-internal`, `security-incident-response`). Mixed vintage on purpose: SLA, refunds and API rate limits exist in a superseded and a current version with different numbers |
| Personal data | None. The internal documents hold operational detail (a duty manager number, verification steps) that must never reach a customer |
| Collection | Written by a language model from outlines (`data-gen/generate_policies.py`) |
| Preprocessing | Chunked and redacted at index build (`nw/policy/chunking.py`); chunk ids are order-independent |
| Known limits | English only, so German questions are answered from English passages; no conflicting documents of the same vintage |
| Uses | Retrieval, answer grounding, evaluation of citation and refusal |
| Maintenance | A changed document changes the index manifest; the service reports `nw_policy_index_stale` until the index is rebuilt. Owner: domain expert |

## Golden sets (data/golden/)

| File | What it is | Size (2026-09-30) | Labelled by | Limits |
| --- | --- | --- | --- | --- |
| `policy_qa.jsonl` | Questions with gold answer, gold chunk ids, `must_refuse`, audience | 77 rows: 17 must refuse, 5 internal audience | Generated from the corpus (`data-gen/generate_golden.py`), reviewed by the author | English only; small |
| `judge_calibration.jsonl` | Answers with human faithfulness and relevance labels, to calibrate the Judge | 30 rows | The author | 30 rows give a wide interval; grow before trusting small score differences |
| `baseline.json` | The policy service's measured baseline, the reference for the gate | 3 top-level keys | `nw.policy.evaluate --write-baseline` | Written on an earlier Workhorse; refresh on the current model |
| `agent_baseline.json` | The agent's per-tier baseline and bars | written 2026-09-28 | `nw.agent.evaluate --write-baseline` | Needs a live refresh on gpt-oss-120b |
| `triage_production.json`, `semantic_production.json` | The committed production model summaries the retrain workflows gate against | one summary each | The promotion step | Rewritten only when training on `data/tickets.jsonl` |
| `triage_slices.jsonl` | Extra P0 and German tickets for slice metrics | 82 rows (58 German, 46 P0) | Written for the slices | Hand-written style differs from the generated corpus |

Adversarial set (`data/adversarial/tickets.jsonl`): 15 cases for the agent gate (prompt
injection in the ticket and in a tool result, entitlement abuse, internal leak, PII, German,
P0, refund limit and others). Personal data: one case carries fictitious PII.

Maintenance for all golden files: a row changes only by a reviewed commit; the manifest hash
makes every change visible. Feedback from `POST /feedback` becomes a candidate row, never a
golden row, until a person commits it. Owner: domain expert, with the QA engineer for the bars.

## PII overlay (data/pii/)

| Question | Answer |
| --- | --- |
| Motivation | Give the privacy lessons something to act on: measure redaction, run a DSAR and an erasure drill, route EU accounts |
| Composition | 600 messages (tickets, multi-turn thread turns, clean negatives) in English and German with 1,678 ground-truth spans over 10 labels, and 120 fictitious data subjects keyed by a pseudonymous `subject_key` |
| Personal data | Fictitious by construction: reserved email domains, fictitious phone ranges, registry specimen IBANs, network test card numbers, documentation IP ranges, invented surnames and towns. Sources in `data/pii/README.md` |
| Collection | `data-gen/generate_pii.py`, seeded, no model calls |
| Known limits | Template text, so a detector can overfit the templates: tune on `dev`, report on `test`. No special category data |
| Uses | Redaction recall and precision (`python -m nw.policy.redact --eval data/pii/messages.jsonl`), DSAR and erasure drills, residency routing tests. Not for training any model |
| Maintenance | Regenerated by the script; the manifest pins it. Owner: privacy-office with the data engineer |
