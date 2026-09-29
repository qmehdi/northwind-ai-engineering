# Cost sheet

> Superseded on 2026-09-29 by `COSTS-platform.md` (ADR 0008 to 0012: the managed platform, cohort and solo modes, open-weight models). Kept for the model price table fetched 2026-09-08 and the network edge prices it cites; the Session path and Reference stack rows below describe infrastructure the course no longer deploys.

Every figure is an estimate from vendor list prices fetched on 2026-09-08 and 2026-09-27 (see the instructor research notes for sources) and must be replaced by the measured figure after the validation run. Model prices: Bedrock global endpoint prices for the AWS track (Sonnet 5 is 3.00 and 15.00 USD per million tokens since 2026-09-01, when launch pricing ended; Opus 5 at 5.00 and 25.00; Haiku 4.5 at 1.00 and 5.00) and first-party list prices for Vertex. The AWS figures below are about 50 percent higher than the same runs on the launch price. Assumptions: cohort of 25, six sessions, us-east-1 and us-central1, participants run in their own accounts.

## Model spend, both tracks

| Item | Tokens per unit | Cost per unit | Per participant per cohort |
| --- | --- | --- | --- |
| Session 1 preflight and live check | under 1k | under 0.01 USD | 0.01 |
| Session 4 judged harness run (77 cases, Opus judge) | about 60 judgements at 4k in, 0.2k out | about 6 USD (measured 6.08 on AWS) | 6 |
| Session 4 unjudged experiments, 3 runs | 77 answers at 3k in, 0.3k out each | under 1 USD each | 2.5 |
| Session 5 adversarial set, hand-built loop | 15 runs at about 5 steps | 1.6 USD measured on AWS at launch pricing, about 2.4 at the current Sonnet 5 price | 2.5 |
| Session 5 framework port | same | under 1 USD | 1 |
| Session 6 capstone runs and demos | 20 tickets through the loop and the same 20 through the router, 4 at a time, plus the latency and rollback drills | about 3 USD | 3 |
| Headroom for reruns and mistakes | | | 5 |
| **Model spend per participant** | | | **about 20 USD** |

The Session 1 spend cap of 10 USD per session is set in `.env`; the Session 4 judged run is the only step that approaches it. The deployed agent runs with `NW_SPEND_CAP_USD=25` on both tracks so the whole capstone fits under one cap.

## AWS track

### Session path (each participant, Session 6)

| Item | Rate | Assumption | Cost |
| --- | --- | --- | --- |
| Lambda compute, arm64 | 0.0000133334 USD per GB-second | about 12,000 GB-seconds: 40 capstone runs on the 8 GB agent at about 30 seconds each, 20 latency runs, the tool functions at 2 to 4 GB behind them, a handful of cold starts | 0.16 USD, inside the 400,000 GB-second free tier |
| Lambda requests | 0.20 USD per million | a few hundred | 0 |
| Ephemeral storage above 512 MB | 0.0000000309 USD per GB-second | 1 GB configured | under 0.01 USD |
| ECR storage | 0.10 USD per GB-month | about 2.5 GB per image with CPU-only torch wheels (the CUDA builds were 6 to 8 GB); the four images share their dependency layers in one repository, about 4 GB stored | 0.40 USD per month |
| Secrets Manager, the API key | 0.40 USD per secret per month, 0.05 USD per 10,000 calls | one secret, read once per cold start | 0.40 USD per month |
| X-Ray with Transaction Search | 0.35 USD per GB of spans, 1 percent indexed free, 100,000 traces free per month | a few MB | 0 |
| CloudWatch Logs ingestion | 0.50 USD per GB after 5 GB free per month | a few MB of JSON logs | 0 |
| Drift alarms: a metric filter per function on the `drift_alert` log line | metric filters are free; four custom metrics sit inside the ten free ones | 4 filters, 4 alarms | 0 |
| Exported service metrics (EMF lines, `Northwind` namespace) | 0.30 USD per custom metric per month after 10 free, prorated by the hour and charged only when a line is written (CloudWatch pricing, fetched 2026-09-28) | 31 series across the four functions (6 each for triage and semantic, 7 for policy, 12 for the agent), written only while a function is invoked: about 2 hours in the session | 0.03 USD in the session; 0 idle, since a frozen function writes nothing; up to 6.30 USD a month under constant traffic |
| CodeDeploy: an application, four deployment groups, the canary shifts | no charge for deployments to Lambda (CodeDeploy pricing, fetched 2026-09-28) | every `cdk deploy` and `deploy_aws.sh rollout` | 0 |
| Published versions and the `live` alias per function | free; a version keeps its image reference, nothing extra is stored | | 0 |
| CloudWatch dashboard, alarms, SNS, Budgets | free tier | | 0 |
| NAT gateway and data transfer | | none by design: no VPC, function URLs, responses of a few KB | 0 |
| Cold start | billed as ordinary duration | first request after idle, measured in Step 6 | 20 to 40 seconds for the agent image |
| **During the 2-hour session** | | | **under 0.10 USD** |
| **Idle per day if left running** | | Lambda bills nothing idle; images and the secret | **0.03 USD** |
| **Idle per month if left running** | | | **about 0.80 USD** |
| After `make stop` (reserved concurrency 0) | | | about 0.80 USD per month |
| After `make destroy` | | | 0 |
| Always warm instead: provisioned concurrency on the agent | 0.0000033334 USD per GB-second, arm64 (x86 is 0.0000041667) | 8 GB, one instance, all day | 0.10 USD per hour, about 2.30 USD per day, about 70 USD per month |

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
| Artifact Registry | 0.10 USD per GB-month | about 2.5 GB per image with CPU-only torch wheels, five images sharing dependency layers, about 5 GB | 0.50 USD per month |
| Secret Manager, the API key | 0.06 USD per active secret version per month after six free, 0.03 USD per 10,000 access operations after 10,000 free | one version, read at startup | 0 |
| Cloud Monitoring, Trace, Logging | free tier | | 0 |
| Drift alerts: one log-based metric over the northwind services and one alert policy | free | | 0 |
| Exported service metrics: 20 log-based metrics (requests, errors, p95, cost, drift per service) and four p95 alert policies | user-defined log-based metrics are Cloud Monitoring custom metrics, charged by bytes ingested after 150 MiB per billing account per month, then 0.2580 USD per MiB (Cloud Monitoring pricing, fetched 2026-09-28) | one point per field per minute of activity, a few KB a day | 0 |
| Cloud Run traffic split (`CANARY=10`), revision tags | free | | 0 |
| **During the 2-hour session** | | | **under 0.20 USD** |
| **Idle per day** | | min instances 0 | **0** |
| Cold start | | first request after idle, measured in Step 6 | 20 to 40 seconds for the agent image |
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

## The network edge, priced but not deployed

Lambda function URLs and a public Cloud Run service have no throttling and no WAF; the course's mitigation is the in-service rate limiter per API key (`nw/ratelimit.py`). The production options, list prices fetched on 2026-09-28:

| Option | Rate | For the course's volume |
| --- | --- | --- |
| AWS API Gateway HTTP API in front of the functions, with usage plans on REST | 1.00 USD per million requests (HTTP API, first 300 million); 3.50 USD per million (REST API); 1 million calls free per month for 12 months | 0 |
| AWS WAF on API Gateway or CloudFront | 5.00 USD per web ACL per month, 1.00 USD per rule per month, 0.60 USD per million requests | about 8 USD per month for one ACL with three rules |
| CloudFront in front of a function URL (origin access control) | Free plan: 1 million requests and 100 GB a month at 0 USD; Pro 15 USD per month; Business 200; Premium 1,000 | 0 on the Free plan |
| GCP Cloud Armor Standard on a load balancer | 0.006849315 USD per hour per security policy (about 5 USD per month), 0.001369863 USD per hour per rule (about 1 USD per month), 0.75 USD per million requests (globally scoped policies), 0.60 (regional) | about 8 USD per month for one policy with three rules |
| GCP external Application Load Balancer in front of Cloud Run | 0.025 USD per hour for the first five forwarding rules (about 18 USD per month), 0.008 USD per GiB inbound processed | about 18 USD per month, the largest fixed cost on either track |
| GCP API Gateway in front of Cloud Run | 0 for the first 2 million calls per month per billing account, 3.00 USD per million from 2 million to 1 billion | 0 |

## Validation run budget

One end-to-end run per track, deploy to destroy on the same day: about 5 USD each on the cloud side plus about 5 USD of model spend each, **about 20 USD total**, with a budget alarm at 50 USD on each account.
