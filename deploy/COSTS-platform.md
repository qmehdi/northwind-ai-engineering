# Cost sheet, managed platform (2026-09-29)

Replaces `COSTS.md` once ADR 0008 to 0012 are accepted. One environment per cloud track; the rows say which part of that environment each component is. Every number was fetched on the date shown from the vendor's pricing page or price list; nothing is estimated from memory. Two modes per cloud track (ADR 0009); the Local track has no cloud cost. Regions: us-east-1 and us-central1 unless the service is global.

Sources, all fetched 2026-09-29 unless a row says otherwise: AWS pricing pages (`aws.amazon.com/<service>/pricing`), the AWS price list files for SageMaker, CodeBuild and Bedrock in us-east-1 (`pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<offer>/current/us-east-1/index.csv`, published 2026-09-28) and the Bedrock foundation model feed the pricing page renders; Google Cloud pricing pages (`cloud.google.com/<product>/pricing`) fetched as HTML with curl because they render client side. Where a vendor page states no price, the cell says which page was checked.

Assumptions shared by every table:

- Cohort of 25 learners plus one instructor tenant, six 2-hour sessions plus Part 0, one session a week.
- The platform lives 45 days (a Part 0 week plus six weekly sessions), 1,080 hours; a month is 730 hours.
- A session day is 8 hours of platform activity (pre-session checks, the session, clean-up); seven session days per cohort.
- Per-second and per-hour components that can be stopped are priced for session days only; the row says what they cost if left running for the 45 days.
- Everything else (registries, buckets, keys, alarms) is priced for the 45 days.

## Model spend, every track

| Role | AWS | Google Cloud | Local | Price per million tokens in and out (fetched 2026-09-29) |
| --- | --- | --- | --- | --- |
| Workhorse | gpt-oss-120b on Bedrock | gpt-oss-120b on the Agent Platform | gpt-oss-120b on Ollama | Bedrock us-east-1 0.15 and 0.60 (price list); Google 0.09 and 0.36 (generative AI pricing page) |
| Economy | Nova Micro | gpt-oss-20b | gpt-oss-20b on Ollama | Nova Micro 0.035 and 0.14 (Bedrock price list, us-east-1); gpt-oss-20b 0.07 and 0.30 on Bedrock us-east-1, 0.07 and 0.25 on Google |
| Judge | Claude Opus 5 on Bedrock (the calibrated judge) | Claude Opus 5 on the Agent Platform | Claude on either cloud, or judge-free | Bedrock us-east-1 in-region 5.50 and 27.50 (Bedrock foundation model feed; the feed carries no global row, the global endpoint is 5.00 and 25.00 per `COSTS.md`, 2026-09-08); Google global 5.00 and 25.00, regional 5.50 and 27.50 (generative AI pricing page) |

Per participant per cohort, the same units as `COSTS.md`:

| Item | Tokens per unit | AWS | Google Cloud |
| --- | --- | --- | --- |
| Part 0 and Session 1 preflight, live check | under 1k | 0.01 | 0.01 |
| Judged harness run, 60 judgements on Opus 5 | 4k in, 0.2k out each | 1.65 (in-region price; 1.50 on the global endpoint) | 1.50 (global; 1.65 regional) |
| Unjudged experiments, 3 runs on the Workhorse | 77 answers at 3k in, 0.3k out each | 0.15 | 0.09 |
| Adversarial set, hand-built loop, 15 runs at 5 steps | 8k in, 0.3k out per step | 0.10 | 0.06 |
| Framework port | same | 0.10 | 0.06 |
| Capstone runs and demos, 40 tickets at 5 steps plus drills | 8k in, 0.3k out per step, router sends part to Economy | 0.28 | 0.17 |
| Headroom for reruns and mistakes | | 1.00 | 1.00 |
| **Model spend per participant** | | **about 3.30 USD** | **about 2.90 USD** |

- The per-step token counts for the agent loops are the counts implied by the measured 1.6 USD for 75 steps at the Sonnet 5 launch price in `COSTS.md`; re-measure on gpt-oss-120b when the baselines are regenerated (ADR 0010).
- The judge is the only Claude call; it is 50 percent of the AWS figure and the reason the target reads "under 3 USD plus the judge".

## AWS track

### Cohort mode: shared platform (deployed once per cohort by the instructor)

| Component | Part of the environment | Unit price (us-east-1, fetched 2026-09-29) | Cohort estimate |
| --- | --- | --- | --- |
| SageMaker domain, managed MLflow tracking server | platform | domain: no charge for creating or configuring a Studio domain (SageMaker AI FAQ); tracking server Small 0.60 USD per hour, Medium 1.04, Large 1.91, metadata storage 0.10 USD per GB-month (price list) | 33.70 USD: Small server running 8 hours on each of 7 session days, 1 GB of metadata; 648 USD if left running 45 days |
| SageMaker Model Registry | platform | no registry line on the SageMaker AI pricing page or FAQ | 0 |
| ECR, artifacts bucket, KMS | platform | ECR 0.10 USD per GB-month (500 MB free for one year); S3 Standard 0.023 USD per GB-month, PUT 0.005 and GET 0.0004 USD per 1,000; KMS 1.00 USD per key per month prorated hourly, 0.03 USD per 10,000 requests after 20,000 free | 6.35 USD: 10 GB of images, 50 GB of artifacts and pipeline outputs, 2 keys, 1.5 months |
| Model gateway (multi-provider gateway guidance, LiteLLM on ECS Fargate, load balancer) | platform | Fargate Linux ARM 0.0324 USD per vCPU-hour and 0.00356 per GB-hour (x86 0.0404 and 0.00444); Application Load Balancer 0.0225 USD per hour plus 0.008 per LCU-hour | 54.30 USD: one 0.5 vCPU 1 GB task (0.0198 USD per hour) plus the load balancer at about 1 LCU (0.0305), 0.0503 USD per hour, 1.21 per day, 1,080 hours |
| AgentCore Gateway, Agent Registry, Identity | platform | Gateway 0.005 USD per 1,000 invocations, search 0.025 per 1,000, 0.02 USD per 100 tools indexed per month; Agent Registry (general availability since 2026-08-31, no longer preview) first 5,000 records, 1 million search and 2 million list or get calls free per month, then 0.40 USD per 1,000 records, 0.02 per 1,000 search, 0.004 per 1,000 list or get; Identity 0.010 USD per 1,000 tokens or keys, free through Runtime or Gateway | 0.10 USD: 12,500 tool invocations, 7 tools, 26 registry records |
| CloudWatch cross-account observability, CloudTrail | platform | cross-account observability at no additional cost; custom metrics 0.30 USD per metric-month for the first 10,000, prorated by the hour and charged only when written; alarms 0.10 USD per alarm-metric per month after 10 free; logs 0.50 USD per GB ingested after 5 GB, 0.03 per GB-month stored; CloudTrail first management trail free, 2.00 USD per 100,000 events after | 26.00 USD: 806 metric series (31 per tenant) written about 22 hours, 100 billable alarm-metrics for 1.5 months, 10 GB of logs |
| Cognito user pool | platform | 10,000 monthly active users free on Lite and Essentials, then 0.0055 (Lite) or 0.015 (Essentials) USD per MAU; Plus 0.020 with no free tier | 0: 26 users |
| SageMaker Pipelines runs (processing, training, evaluation) per tenant | tenant | Pipelines: no additional charge, pay for the instances (SageMaker AI FAQ); ml.m5.large 0.115 USD per hour and ml.m5.xlarge 0.23 for Processing, Training and Hosting (price list) | 0.70 USD per tenant: 8 runs at 0.087 USD (processing 10 minutes on ml.m5.large, training 15 minutes on ml.m5.xlarge, evaluation 5 minutes on ml.m5.large), 3 for Project 1, 2 for Project 2, 3 reruns |
| SageMaker Serverless Inference per tenant | tenant | 0.00002 USD per GB-second, 0.00004 USD per second at 2 GB (price list, memory sizes 1 to 6 GB); no per-request line in the price list; zero when idle | 0.20 USD per tenant: two endpoints (triage, semantic) at 2 GB, 250 seconds per session and 1,000 seconds of cold starts each |
| Knowledge Base on S3 Vectors per tenant | tenant | S3 Vectors 0.06 USD per GB-month, PUT 0.20 USD per GB, queries 2.50 USD per million plus 0.004 USD per TB processed under 100,000 vectors and 0.01 USD per GB returned; self-managed Knowledge Bases list no retrieval charge (the managed Knowledge Base tier is 5.00 USD per GB-month of index and 1.00 USD per 1,000 Retrieve calls and is not used) | under 0.01 USD per tenant: 212 chunks, 1,000 queries |
| Bedrock Prompt Management | tenant | no Prompt Management line on the Bedrock pricing page (Prompt Optimization, not used, is 0.03 USD per 1,000 tokens); only the inference of the model chosen when testing a prompt | 0 |
| AgentCore Runtime, Memory, Observability, Evaluations per tenant | tenant | Runtime v1 0.0895 USD per vCPU-hour and 0.00945 per GB-hour, per second while active (v2 consumption 0.1276 and 0.0169, committed baseline 0.0997 and 0.0132 from October 2026); Memory 0.25 USD per 1,000 new events, 0.75 per 1,000 records stored per month, 0.50 per 1,000 retrievals; Observability at CloudWatch rates; Evaluations built-in 0.0024 USD per 1,000 input and 0.012 per 1,000 output tokens, custom 1.50 USD per 1,000 evaluations | 1.61 USD per tenant: 2 active hours at 1 vCPU 2 GB (0.22), 500 events and 50 records (0.17), 50 MB of spans (0.02), 100 built-in evaluations at 4k in and 0.2k out (1.20) |
| CodePipeline, CodeBuild, CodeDeploy | delivery | CodePipeline V2 0.002 USD per action-minute after 100 free per month (V1 1.00 USD per active pipeline after one free); CodeBuild arm1.small 0.0034 USD per minute, general1.small 0.005, 100 free minutes per month; CodeDeploy no charge for Lambda, ECS and EC2 deployments, 0.02 USD per on-premises instance update (FAQ); CodeConnections: no price published, `aws.amazon.com/codeconnections/pricing` returns 404 and the CodePipeline pricing page bills the connection only as source action minutes | 5.40 USD: 26 tenants, 4 runs each of 15 action-minutes and 8 arm1.small build-minutes |
| Real-time SageMaker endpoint with CodeDeploy canary | live target | ml.m5.large 0.115 USD per hour per variant; the canary is a second variant for its duration | 7.80 USD: one shared endpoint 8 hours on 7 session days plus 2 canary hours per session; 124 USD if left running 45 days |
| AgentCore Runtime, guardrail, policy engine | live target | Guardrails per 1,000 text units: content filters 0.15 USD, denied topics 0.15, sensitive information 0.10, contextual grounding 0.10 (prompt attack through InvokeGuardrailChecks 0.08); Policy 0.000025 USD per authorization, authoring 0.13 USD per 1,000 tokens; Runtime as above | 4.40 USD: 5,000 guarded requests at 2 text units through content filters, PII and grounding (3.50), 5,000 authorizations (0.13), 7 demo hours of runtime (0.76) |
| Budgets and alarms | platform | budget monitoring and notifications free of charge; action-enabled budgets 0.10 USD per day after the first two; alarms priced in the CloudWatch row | 0 |

- Platform, delivery and live target rows add to **about 138 USD per cohort**, 5.52 per learner.
- Tenant rows add to **2.52 USD per tenant**; with model spend the **per-tenant cost is about 5.80 USD**.
- Cohort total: about 280 USD (138 plus 25 times 5.80).

Credit to request per participant (cohort): platform share 5.52 plus per-tenant 5.80, 11.35 USD, **request 15 USD per learner** on the instructor's account; the whole cohort fits under a 300 USD budget alarm.

### Solo mode

Platform plus one tenant in one account; the learner deploys before a session and destroys after it.

| Item | Cost |
| --- | --- |
| Platform per session day (8 hours): MLflow Small 4.80, gateway task and load balancer 0.40, endpoint plus canary 1.15, guardrail 0.14, KMS 0.02, delivery run inside the free tiers | about 6.60 USD |
| Seven session days | 46.20 USD |
| Kept between sessions for 45 days: 4 GB in ECR, 2 GB in S3, 2 KMS keys | 3.70 USD |
| Tenant rows | 2.52 USD |
| Model spend | 3.30 USD |
| **Solo cost for one learner** | **about 56 USD** |
| If the platform is left running for 45 days instead (MLflow 648, gateway 54, endpoint 124, keys 3) | about 836 USD |
| **Daily idle cost of a deployed platform**: MLflow Small 14.40, gateway 1.21, endpoint 2.76, 100 alarms 0.33, KMS 0.07, ECR and S3 0.07; Serverless Inference and AgentCore bill nothing idle | **18.84 USD per day**; 1.68 per day with the tracking server stopped and the endpoint deleted |
| **Residual after destroy**: retained artifacts bucket (50 GB, 1.15 per month), retained log groups (10 GB, 0.30 per month), ECR if retained (1.00 per month); KMS keys in their deletion window at most 1.00 per key per month (the KMS page does not say whether pending keys bill) | **about 2.50 USD per month**, plus at most 2.00 for the key deletion window |

## Google Cloud track

### Cohort mode

| Component | Part of the environment | Unit price (us-central1, fetched 2026-09-29) | Cohort estimate |
| --- | --- | --- | --- |
| Artifact Registry, Cloud Storage, KMS | platform | Artifact Registry 0.000136986 USD per GiB-hour (0.10 per GB-month) after 0.5 GB free; Cloud Storage Standard 0.000027397 USD per GiB-hour (0.020 per GB-month), Class A 0.005 and Class B 0.0004 USD per 1,000, 5 GB-months and 5,000 Class A free; Cloud KMS 0.06 USD per active software key version per month (0.000082192 per hour), 0.03 USD per 10,000 operations after 10,000 free | 3.00 USD: 10 GB of images, 50 GB of artifacts, 2 key versions, 1.5 months |
| Model gateway (LiteLLM on Cloud Run) | platform | instance-based billing 0.000018 USD per vCPU-second and 0.000002 per GiB-second, free tier 240,000 vCPU-seconds and 450,000 GiB-seconds per month (`COSTS.md`, 2026-09-08); request-based rates in `COSTS.md` | 70.00 USD: one always-warm instance of 1 vCPU 1 GiB (0.072 USD per hour, 1.73 per day) for 1,080 hours, minus the free tier |
| Cloud Monitoring, Logging, Trace, Security Command Center | platform | Monitoring 0.2580 USD per MiB after 150 MiB per billing account; Logging 0.50 USD per GiB after 50 GiB per project, retention 0.01 USD per GiB-month beyond 30 days; Trace 0.20 USD per million spans after 2.5 million; alerting policies 0.35 USD per month per metric reference effective 2027-09-01; Security Command Center Standard tier free of charge | 0: 520 log-based metrics under 150 MiB, logs under 50 GiB, spans under 2.5 million |
| Vertex AI Pipelines runs per tenant | tenant | 0.03 USD per pipeline run plus the compute of each step; custom training e2-standard-4 0.154126276 USD per hour (the smallest E2 in the training table) | 0.86 USD per tenant: 8 runs at 0.107 USD (run fee plus 30 step-minutes on e2-standard-4) |
| Vertex AI Experiments, Model Registry | tenant | Experiments: no charge line; ML Metadata 10 USD per GiB-month prorated; TensorBoard 10 USD per GiB-month if used; Model Registry: no cost for models in the registry, cost only on deployment | under 0.01 USD per tenant |
| Container serving from the registry artifact on Cloud Run per tenant | tenant | request-based billing (`COSTS.md`, 2026-09-08): 0.000024 USD per vCPU-second and 0.0000025 per GiB-second while serving, 2 million requests and 180,000 vCPU-seconds free per month | 0.50 USD per tenant: 6 sessions at 0.15 before the free tier |
| RAG Engine on Vector Search per tenant | tenant, shared endpoint | RAG Engine itself has no price (RAG Engine billing page: ingestion, default parser and fixed-size chunking free; you pay the embedding model, the vector database and any reranker); Vector Search e2-standard-2 0.0938084 USD per node-hour, index build 3.00 USD per GiB, streaming updates 0.45 USD per GiB; RagManagedDb Basic tier is a Spanner Enterprise instance of 100 processing units with backup, Spanner Enterprise regional 1.23 USD per node-hour (1,000 units), 0.123 USD per hour for 100 units | 101.30 USD for the cohort: one index endpoint on one e2-standard-2 node for 1,080 hours, corpora per tenant on it (4.05 per tenant); a node per tenant would be 2,533 USD; RagManagedDb instead: 132.80 USD |
| Agent Engine runtime, sessions, Memory Bank per tenant | tenant | 0.0864 USD per vCPU-hour, 0.0090 per GB-hour, free tier 50 vCPU-hours and 100 GB-hours per month, 0.25 USD per 1,000 session events (2026-09-08) | 0.13 USD per tenant: 2 active hours at 1 vCPU 2 GB (0.21, inside the project free tier for the cohort) and 500 events (0.13) |
| Cloud Build, Cloud Deploy | delivery | Cloud Build e2-standard-2 0.006 USD per build-minute, e2-medium 0.003, 2,500 free build-minutes per billing account per month; Cloud Deploy: first active multiple-target delivery pipeline free, 5.00 USD per additional active multiple-target pipeline per month, single-target pipelines not charged | 0: 832 build-minutes, one single-target pipeline |
| Vertex endpoint with traffic split | live target | e2-standard-2 0.0770564 USD per node-hour, n1-standard-2 0.1095 (online prediction table); the split adds a node | 5.25 USD: one shared e2-standard-2 endpoint 8 hours on 7 session days plus 2 split hours per session; 83 USD if left running 45 days |
| Model Armor | live target | 0.10 USD per million tokens, first 2 million per month free (2026-09-08) | 0.30 USD: 5,000 requests at 1,000 tokens |
| Budgets and alerting | platform | Cloud Billing budgets carry no price on the Monitoring pricing page; alert policies free until 2027-09-01 | 0 |

- Platform, delivery, live target and the shared Vector Search endpoint add to **about 180 USD per cohort**, 7.20 per learner.
- Tenant rows add to **1.50 USD per tenant**; with model spend the **per-tenant cost is about 4.40 USD**.
- Cohort total: about 290 USD (180 plus 25 times 4.40). Vector Search is 56 percent of the platform; RAG Engine's serverless mode (public preview, RAG Engine overview page) would remove it once it is generally available.

Credit to request per participant (cohort): platform share 7.20 plus per-tenant 4.40, 11.60 USD, **request 15 USD per learner**. New accounts carry 300 USD of Google credits for 90 days (fetched 2026-09-29), which covers solo mode for one learner even with the platform left running.

### Solo mode

As above in one project; the learner deploys before a session and destroys after it.

| Item | Cost |
| --- | --- |
| Platform per session day (8 hours): gateway 0.58, Vector Search node 0.75, endpoint plus split 0.77 | about 2.10 USD |
| Seven session days | 14.70 USD |
| Kept between sessions for 45 days: 4 GB in Artifact Registry, 2 GB in Cloud Storage, 2 key versions, less the free tiers | 0.55 USD |
| Tenant rows | 1.50 USD |
| Model spend | 2.90 USD |
| **Solo cost for one learner** | **about 20 USD** |
| If the platform is left running for 45 days instead | about 184 USD, inside the 300 USD credit |
| **Daily idle cost of a deployed platform**: gateway 1.73, Vector Search node 2.25, endpoint 1.85, storage 0.06 | **5.89 USD per day**; 1.79 per day with the endpoint and the index undeployed |
| **Residual after destroy**: retained bucket (50 GB, 1.00 per month) and registry (10 GB, 0.95 per month); logs inside the free allotment | **about 2 USD per month** |

## Local track

No cloud cost. Requirements to state: 16 GB RAM, about 40 GB of disk for images and models, gpt-oss-20b runs on CPU, gpt-oss-120b needs a GPU or is swapped for gpt-oss-20b in the Workhorse role by a compose profile.

## Validation run budget

One deploy-to-destroy per cloud track and mode, four runs, each on one day with the platform up 8 hours; waits for the go-ahead per track.

| Run | Cloud side | Model spend | Budget |
| --- | --- | --- | --- |
| AWS cohort mode: platform plus two tenants | 6.70 platform day plus 2 times 2.52 tenant rows | 2 times 3.30 | 20 USD |
| AWS solo mode: platform plus one tenant | 6.70 plus 2.52 | 3.30 | 15 USD |
| Google Cloud cohort mode: platform plus two tenants | 2.20 platform day plus 2 times 1.50 | 2 times 2.90 | 15 USD |
| Google Cloud solo mode | 2.20 plus 1.50 | 2.90 | 10 USD |
| **Total** | | | **60 USD**, budget alarm at 100 USD on each account; the Google runs fit in a new account's 300 USD credit |

Prices not found on a vendor page (2026-09-29): CodeConnections (pricing URL returns 404, no charge stated anywhere on aws.amazon.com); SageMaker Serverless Inference has no per-request price in the price list, only per second by memory size; the Bedrock feed has no global-endpoint row for Claude Opus 5 (in-region 5.50 and 27.50 fetched, global 5.00 and 25.00 carried from 2026-09-08); Cloud Run and Agent Engine rates are carried from 2026-09-08.
