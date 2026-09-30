# Northwind data

Everything here is synthetic, generated once for the course and committed so every participant trains and evaluates on identical data. Nothing is real customer data.

## tickets.jsonl

One JSON object per line. 7,950 support tickets for Northwind Cloud.

| Field | Type | Meaning |
| --- | --- | --- |
| `ticket_id` | string | `T-100000` upwards |
| `account_id` | string | Key into `accounts.json` |
| `created_at` | ISO 8601 | Synthetic, July 2025 to June 2026 |
| `subject`, `body` | string | What the customer wrote |
| `answer` | string | The support agent's first reply. Used in Project 4 as the reference for the resolution agent |
| `type` | enum | Incident, Request, Problem, Change |
| `queue` | enum | Where the ticket was routed. About 12 percent are deliberately misrouted |
| `priority` | enum | P0 to P3. The Project 1 target |
| `tags` | list | 3 to 7 tags from a fixed vocabulary. The Project 2 multi-label target |
| `language` | enum | `en` or `de` |
| `messy` | list | Flags describing deliberate noise: `typos`, `pasted_log`, `buried_problem`, `empty_subject`, `all_caps`, `very_short`, `angry`. Empty for clean tickets |
| `product` | string | Product area. Not a target; useful for error analysis |
| `split` | enum | `train`, `val`, `test`, stratified by priority, 80/10/10 |

Do not train on `answer`, `messy`, `product` or `split` as features. They are there for evaluation and analysis.

## Provenance and the priority rule

The schema and the label distributions are modelled on the public dataset Tobi-Bueck/customer-support-tickets on Hugging Face (English subset), which has three priority levels: high 39 percent, medium 41 percent, low 20 percent. Northwind's four levels are derived by a rule:

| Northwind | Derived from | Share |
| --- | --- | --- |
| P0 | Incidents about outage, data loss or security, carved out of "high" | 4 percent |
| P1 | The rest of "high" | 35 percent |
| P2 | "medium" | 41 percent |
| P3 | "low" | 20 percent |

The text was written by a language model from briefs whose labels were fixed in code, so the distribution is exact and no row of the reference dataset was copied.

## accounts.json

240 customer accounts: `account_id`, `company`, `tier` (Starter, Pro, Enterprise), `region`, `seats`, `industry`, `features`, `sla_hours`. The agent's `lookup_customer` and `check_entitlement` tools (Project 4) read this file. Accounts carry no people; contact persons live only in the PII overlay (`pii/`).

## policies/

The Northwind policy corpus for Project 3: markdown documents with a metadata block (`doc_id`, `audience`, `effective`, `supersedes`). Some topics exist in two versions with different numbers; the older one is superseded and retrieval must prefer the current one. Documents with `audience: internal` must never be quoted to a customer. `index.json` lists them.

## pii/

A fictitious PII overlay: 600 ticket-like messages (English and German, single tickets and multi-turn threads) with ground-truth spans, and the 120 fictitious people they mention. For measuring redaction, DSAR and erasure drills, and residency routing. Never training data. Format, value sources and the redactor baseline are in `pii/README.md`; datasheets for every dataset are in `docs/governance/datasheets.md`.

## agents/ and use_cases.yaml

The use case catalog (`use_cases.yaml`, checked by `python -m nw.agent.catalog --check`) and the agent cards (`agents/*.json`, written by `python -m nw.agent.registry --write`). Each use case carries its EU AI Act class, the Article 50 disclosure, a DPIA reference and a privacy approver.

## Distribution

7,950 tickets after generation (5 of 800 batches were skipped by the generator's count check).

| Priority | Count | Share |
| --- | ---: | ---: |
| P2 | 3295 | 41.4% |
| P1 | 2571 | 32.3% |
| P3 | 1770 | 22.3% |
| P0 | 314 | 3.9% |

| Type | Count | Share |
| --- | ---: | ---: |
| Incident | 3391 | 42.7% |
| Request | 2172 | 27.3% |
| Problem | 1612 | 20.3% |
| Change | 775 | 9.7% |

| Queue | Count | Share |
| --- | ---: | ---: |
| Technical Support | 2309 | 29.0% |
| Product Support | 2158 | 27.1% |
| Customer Service | 1033 | 13.0% |
| Account and Access | 812 | 10.2% |
| Billing and Payments | 587 | 7.4% |
| Security and Compliance | 587 | 7.4% |
| Service Outages | 315 | 4.0% |
| Sales and Renewals | 149 | 1.9% |

| Language | Count | Share |
| --- | ---: | ---: |
| en | 7562 | 95.1% |
| de | 388 | 4.9% |

| Split | Count | Share |
| --- | ---: | ---: |
| train | 6361 | 80.0% |
| val | 795 | 10.0% |
| test | 794 | 10.0% |

| Messiness | Count | Share |
| --- | ---: | ---: |
| clean | 7132 | 89.7% |
| all_caps | 90 | 1.1% |
| empty_subject | 86 | 1.1% |
| typos | 81 | 1.0% |
| angry | 80 | 1.0% |
| very_short | 77 | 1.0% |
| buried_problem | 75 | 0.9% |
| pasted_log | 73 | 0.9% |
| angry, typos | 11 | 0.1% |
| pasted_log, very_short | 11 | 0.1% |
| empty_subject, pasted_log | 11 | 0.1% |
| buried_problem, very_short | 10 | 0.1% |
| very_short, typos | 9 | 0.1% |
| pasted_log, all_caps | 8 | 0.1% |
| buried_problem, empty_subject | 8 | 0.1% |
| buried_problem, typos | 8 | 0.1% |
| pasted_log, empty_subject | 8 | 0.1% |
| very_short, empty_subject | 8 | 0.1% |
| all_caps, angry | 8 | 0.1% |
| very_short, buried_problem | 8 | 0.1% |
| buried_problem, pasted_log | 7 | 0.1% |
| typos, empty_subject | 7 | 0.1% |
| buried_problem, all_caps | 7 | 0.1% |
| all_caps, buried_problem | 6 | 0.1% |
| empty_subject, all_caps | 6 | 0.1% |
| angry, buried_problem | 6 | 0.1% |
| typos, angry | 6 | 0.1% |
| all_caps, very_short | 6 | 0.1% |
| buried_problem, angry | 5 | 0.1% |
| very_short, pasted_log | 5 | 0.1% |
| very_short, angry | 5 | 0.1% |
| empty_subject, very_short | 5 | 0.1% |
| empty_subject, buried_problem | 5 | 0.1% |
| typos, pasted_log | 5 | 0.1% |
| typos, buried_problem | 5 | 0.1% |
| empty_subject, angry | 5 | 0.1% |
| empty_subject, typos | 5 | 0.1% |
| all_caps, empty_subject | 5 | 0.1% |
| typos, very_short | 5 | 0.1% |
| angry, very_short | 4 | 0.1% |
| pasted_log, buried_problem | 4 | 0.1% |
| pasted_log, angry | 4 | 0.1% |
| typos, all_caps | 4 | 0.1% |
| pasted_log, typos | 4 | 0.1% |
| angry, all_caps | 4 | 0.1% |
| angry, pasted_log | 4 | 0.1% |
| very_short, all_caps | 4 | 0.1% |
| all_caps, pasted_log | 4 | 0.1% |
| angry, empty_subject | 3 | 0.0% |
| all_caps, typos | 3 | 0.0% |

| Tag | Count |
| --- | ---: |
| Feature | 2423 |
| Performance | 2175 |
| Feedback | 1734 |
| How-To | 1733 |
| Escalation | 1691 |
| Bug | 1675 |
| Documentation | 1652 |
| Login | 1190 |
| Duplicate | 717 |
| Email | 715 |
| Alerting | 699 |
| Upload | 664 |
| Backup | 654 |
| Quota | 649 |
| Retention | 640 |
| Data Quality | 633 |
| Connector | 632 |
| Push | 625 |
| Audit Log | 624 |
| Deactivation | 624 |
| Offline | 617 |
| Allowlist | 615 |
| Latency | 614 |
| Provisioning | 614 |
| Crash | 611 |
| Settings | 610 |
| Sync | 598 |
| Permissions | 589 |
| Slack | 588 |
| SSO | 580 |
| Jira | 578 |
| MFA | 568 |
| Okta | 564 |
| Salesforce | 562 |
| Seats | 551 |
| Deprecation | 551 |
| Webhook | 543 |
| Rate Limit | 538 |
| Payment | 537 |
| SDK | 537 |
| Invoice | 535 |
| API | 532 |
| Plan | 529 |
| Dashboard | 518 |
| Export | 516 |
| Chart | 513 |
| Refund | 507 |
| Sharing | 498 |
| Breach | 95 |
| Security | 87 |
| Data Loss | 71 |
| Outage | 61 |
