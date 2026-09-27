# Cost sheet

Every figure is an estimate from vendor list prices fetched on 2026-09-08 and 2026-09-27 (see the instructor research notes for sources) and must be replaced by the measured figure after the validation run. Model prices: Bedrock global endpoint prices for the AWS track (Sonnet 5 is 3.00 and 15.00 USD per million tokens since 2026-09-01, when launch pricing ended; Opus 5 at 5.00 and 25.00; Haiku 4.5 at 1.00 and 5.00) and first-party list prices for Vertex. The AWS figures below are about 40 percent higher than the same runs on the launch price. Assumptions: cohort of 25, six sessions, us-east-1 and us-central1, participants run in their own accounts.

## Model spend, both tracks

| Item | Tokens per unit | Cost per unit | Per participant per cohort |
| --- | --- | --- | --- |
| Session 1 preflight and live check | under 1k | under 0.01 USD | 0.01 |
| Session 4 judged harness run (77 cases, Opus judge) | about 60 judgements at 4k in, 0.2k out | about 6 USD (measured 6.08 on AWS) | 6 |
| Session 4 unjudged experiments, 3 runs | 77 answers at 3k in, 0.3k out each | under 1 USD each | 2.5 |
| Session 5 adversarial set, hand-built loop | 15 runs at about 5 steps | 1.6 USD measured on AWS at launch pricing, about 2.4 at the current Sonnet 5 price | 2.5 |
| Session 5 framework port | same | under 1 USD | 1 |
| Session 6 capstone runs and demos | 50 tickets routed | about 2 USD | 2 |
| Headroom for reruns and mistakes | | | 5 |
| **Model spend per participant** | | | **about 20 USD** |

The Session 1 spend cap of 10 USD per session is set in `.env`; the Session 4 judged run is the only step that approaches it.

## AWS track

### Session path (each participant, Session 6)

| Item | Rate | Assumption | Cost |
| --- | --- | --- | --- |
| Lambda compute, arm64 | 0.0000133334 USD per GB-second | about 5,400 GB-seconds: agent 8 GB, policy and semantic 4 GB, triage 2 GB, four cold starts, 50 routed tickets | 0.07 USD, inside the 400,000 GB-second free tier |
| Lambda requests | 0.20 USD per million | a few hundred | 0 |
| Ephemeral storage above 512 MB | 0.0000000309 USD per GB-second | 1 GB configured | under 0.01 USD |
| ECR storage | 0.10 USD per GB-month | about 4 GB of images | 0.40 USD per month |
| Secrets Manager, the API key | 0.40 USD per secret per month, 0.05 USD per 10,000 calls | one secret, read once per cold start | 0.40 USD per month |
| X-Ray with Transaction Search | 0.35 USD per GB of spans, 1 percent indexed free, 100,000 traces free per month | a few MB | 0 |
| CloudWatch Logs, dashboard, alarms, SNS, Budgets | free tier | | 0 |
| **During the 2-hour session** | | | **under 0.10 USD** |
| **Idle per day if left running** | | Lambda bills nothing idle; images and the secret | **0.03 USD** |
| **Idle per month if left running** | | | **about 0.80 USD** |
| After `make stop` (reserved concurrency 0) | | | about 0.80 USD per month |
| After `make destroy` | | | 0 |
| Always warm instead: provisioned concurrency on the agent | 0.0000041667 USD per GB-second | 8 GB, one instance, all day | 0.12 USD per hour, about 2.90 USD per day, about 88 USD per month |

Credit to request per participant for the AWS track: 20 USD of model spend plus 5 USD headroom, **25 USD**, assuming they destroy after the session. Say that in the pre-session message. App Runner, the service this path used first, is closed to new customers; ECS Express Mode is the always-warm alternative and would add about 1.20 USD per day for four small Fargate tasks plus the load balancer.

### Reference stack (deployed once, shown by the instructor)

| Item | Rate | Assumption | Cost |
| --- | --- | --- | --- |
| Session path functions | as above | | 0.03 USD per day idle |
| AgentCore Runtime, 2 runtimes | 0.0895 USD per vCPU-hour active, 0.00945 per GB-hour | billed per second only while a session is active; 1 hour of demos a day at 1 vCPU 2 GB each | 0.22 USD per day |
| AgentCore Gateway | 0.005 USD per 1,000 invocations, 0.02 USD per 100 tools per month | 1,000 calls per day, 7 tools | 0.005 USD per day plus 0.02 USD per month |
| Policy engine | 0.000025 USD per authorization | 1,000 per day | 0.025 USD per day |
| Bedrock Guardrails | 0.15 USD per 1,000 text units for prompt attack, 0.10 for PII and grounding | 1,000 requests per day, 2 units each | 0.70 USD per day |
| S3 Vectors | 0.06 USD per GB-month, 2.50 USD per million queries | 212 chunks, 1,000 queries per day | under 0.01 USD per day |
| Spans through Transaction Search | 0.35 USD per GB ingested, 1 percent indexed free | 200 MB per day | 0.07 USD per day |
| **Upfront** | | ECR push, guardrail version | 0 |
| **One end-to-end validation run** | | deploy, 15 adversarial tickets through the gateway, demo, destroy same day | **about 3 USD plus model spend, about 5 USD** |
| **Per day, demos only** | | | **about 1 USD** |
| **Per month if left running** | | | **about 35 USD**, most of it the guardrail at demo volume |
| After `make stop` | functions refuse invocations, runtimes idle bill nothing | | under 1 USD per month |
| After `make destroy` | | | 0 |

Argus, the reference project this course borrows from, cost about 400 USD a month idle because of an always-on Fargate observability stack and Aurora. This stack has neither: observability is native and retrieval is S3 Vectors.

## GCP track

### Session path (each participant, Session 6)

| Item | Rate | Assumption | Cost |
| --- | --- | --- | --- |
| Cloud Run, request-based billing | 0.000024 USD per vCPU-second, 0.0000025 per GiB-second while serving | 5 vCPU serving 20 minutes of the session | 0.15 USD |
| Cloud Run idle, min instances 0 | 0 | | 0 |
| Cloud Run free tier | 2M requests, 180k vCPU-seconds per month | covers the session | 0 |
| Artifact Registry | 0.10 USD per GB-month | about 4 GB | 0.40 USD per month |
| Cloud Monitoring, Trace, Logging | free tier | | 0 |
| **During the 2-hour session** | | | **under 0.20 USD** |
| **Idle per day** | | min instances 0 | **0** |
| Cold start | | first request after idle, measured in Step 5 | 20 to 40 seconds for the agent image |
| After `make destroy` | | | 0 |

Credit to request per participant for the GCP track: 20 USD of model spend plus 5 USD headroom, **25 USD**.

### Reference stack

| Item | Rate | Assumption | Cost |
| --- | --- | --- | --- |
| Agent Engine runtime | 0.0864 USD per vCPU-hour, 0.0090 per GB-hour, free tier 50 vCPU-hours and 100 GB-hours a month | 1 hour of demos a day at 2 vCPU and 4 GB | 0.21 USD per day, inside the free tier for a demo month |
| Model Armor | 0.10 USD per million tokens, 2 million free per month | 1,000 requests per day at 1,000 tokens | free within the allowance, 0.10 USD per day beyond |
| MCP server on Cloud Run | request-based | 1,000 calls per day | under 0.05 USD per day |
| Cloud SQL with pgvector, optional | db-custom-1-3840 Enterprise zonal | only when `enable_pgvector` | about 50 USD per month |
| **One end-to-end validation run** | | deploy, adversarial set, demo, destroy same day | **about 2 USD plus model spend, about 5 USD** |
| **Per month, demos only, no Cloud SQL** | | | **under 10 USD**, most of it Cloud Run for the MCP server and the session services |
| After `make destroy` | | | 0 |

Vertex AI Vector Search was rejected as the managed retriever because it bills per serving node-hour with a floor of several hundred USD a month for a modest index.

## Validation run budget

One end-to-end run per track, deploy to destroy on the same day: about 5 USD each on the cloud side plus about 5 USD of model spend each, **about 20 USD total**, with a budget alarm at 50 USD on each account.
