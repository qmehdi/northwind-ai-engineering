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

## Azure track

Region eastus2 (the default of `deploy/azure`), prices fetched 2026-09-29. Sources: the Azure Retail Prices API (`https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&currencyCode=USD`, filtered on `armRegionName eq 'eastus2'` and the service name, pay-as-you-go consumption rows; no key needed) and, for what the API does not carry, the pricing pages `azure.microsoft.com/en-us/pricing/details/<service>/` (container-apps, monitor, cost-management, cognitive-services/content-safety, foundry-agent-service, ai-foundry-models/aoai) and Microsoft Learn's Claude billing page (`learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models-billing`, updated 2026-09-11). The Foundry models pricing page renders "$-" for every price; the API rows are the ones used. Same assumptions as the other tracks: 25 learners plus one instructor tenant, 45 days (1,080 hours), seven 8-hour session days.

### Model spend

| Role | Deployment (`deploy/azure/main.bicep`, `models`) | Price per million tokens in and out, Global Standard |
| --- | --- | --- |
| Workhorse | `gpt-oss-120b` (format OpenAI-OSS, version 1) | 0.15 and 0.60 (Foundry Models, API) |
| Economy | `mistral-small-2503` (format Mistral AI, version 1) | 0.10 and 0.30: sold through Azure Marketplace, so the Retail Prices API has no inference row; the rate is the Foundry catalogue price as third-party trackers record it (futureagi.com, getmaxim.ai, verified 2026-08-06), confirm in the portal's Pricing tab in the delivery week. Rejected, all in eastus2 Global Standard: gpt-5.4-nano 0.20 and 1.25 (API), dearer than the Workhorse; gpt-5-nano 0.05 and 0.40 (API) retires 2027-02-09; gpt-4.1-nano retires 2026-10-14; Phi-4-mini-instruct 0.075 and 0.30 (API) has no tool calling; Ministral-3B 0.04 and 0.04 (trackers) calls tools but is a 3B model for a multi-step agent loop |
| Judge | `claude-opus-5` (format Anthropic, version 2) | 5.00 and 25.00, cache reads 0.50 and 5-minute cache writes 6.25: Claude has no Azure price meter; it is billed through Azure Marketplace in Claude Consumption Units of 0.01 USD at Anthropic's list rates (Learn billing page); US Data Zone deployments cost 1.1 times |
| Embeddings | `text-embedding-3-small` | 0.02 per million (API, `text-embedding-3-small-glbl`) |

Per participant, the units of the model spend table above: preflight 0.01; judged harness, 60 judgements on Opus 5, 1.50; unjudged experiments 0.15; adversarial set 0.10; framework port 0.10; capstone 0.27 (a third of the steps routed to Economy, about two thirds of the Workhorse's cost per step); embeddings for the policy corpus and queries 0.02; headroom 1.00. **Model spend per participant: about 3.15 USD.** The Economy role is cheaper than the Workhorse on both input and output, which is the point of the cheap-first router; `models` in `main.bicep` and `NW_MODEL_ECONOMY` swap it in one line each.

Content filters with Prompt Shields on the deployments carry no separate meter in the price list; the standalone Content Safety API would be 0.375 USD per 1,000 text records after 5,000 free a month, Prompt Shields included (Content Safety page). The Foundry Agent Service states "no additional charge for creating or running Foundry-native agents using prompts and workflows" (Agent Service page); hosted agents, file search and code interpreter show "$-" (not published). Foundry evaluations have no price of their own; the Azure ML evaluation token meters are 0.02 USD per 1,000 input and 0.06 per 1,000 output tokens (API), and the course's judged evaluations run on the Judge deployment, counted above.

### Cohort mode: shared platform (deployed once per cohort by the instructor)

| Component | Part of the environment | Unit price (eastus2, fetched 2026-09-29) | Cohort estimate |
| --- | --- | --- | --- |
| API Management Basic v2, the AI gateway | platform | 0.20548 USD per hour, 10 million calls included, then 0.03 per 10,000; the "AI Gateway Requests" meter is 0; Developer 0.0658 per hour, Standard v2 0.9589; Consumption (0.035 per 10,000 after 1 million free) has no llm-token-limit | 221.92 USD: 1,080 hours; the tier cannot be paused |
| Azure AI Search | platform | Basic 0.101 USD per search unit hour (15 indexes), Standard S1 0.336 (50 indexes); semantic ranker free plan | 362.88 USD: S1, because 27 owners need 27 indexes; 109.08 on Basic for 14 tenants or fewer |
| Container Registry | platform | Basic 0.1666 USD per day, Standard 0.6666; storage 0.10 USD per GB-month | 10.50 USD: 45 days plus 20 GB of images for 1.5 months, before any storage the tier includes |
| Storage: the lake (hierarchical namespace, Hot LRS) and the workspace account (flat, Hot LRS) | platform | lake 0.018 USD per GB-month, writes 0.065 and reads 0.005 per 10,000; flat 0.0184 per GB-month, writes 0.05 and reads 0.004 per 10,000 | 7.30 USD: 50 GB in the lake and 30 GB in the workspace account for 1.5 months, 700,000 writes, 3 million reads |
| Key Vault Standard | platform | 0.03 USD per 10,000 operations | 0.60 USD: 200,000 secret reads (every app start reads two) |
| Log Analytics and Application Insights (workspace based) | platform | 2.76 USD per GB ingested after 5 GB free per billing account per month; 31 days retention included | 20.70 USD: 15 GB over the cohort (the workspace caps at 1 GB a day), 7.5 GB inside the free allowance |
| Alerts, action group, workbook, budget | platform | metric alerts: first 10 time series free, then 0.10 USD each per month; log search alert at 15 minutes 0.50 per month; email notifications first 1,000 free; workbooks: no meter and no price on the Monitor page (not published); budgets free (Cost Management page) | 0.75 USD: four metric alerts inside the free series, one drift log alert |
| Container Apps (consumption) for every owner's policy, agent and MCP apps | platform and tenant | 0.000024 USD per active vCPU-second, 0.000003 per GiB-second, 0.40 per million requests; 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests free per subscription per month (Container Apps page); scale to zero, no environment fee on the consumption plan | 5.83 USD: 2 active hours per tenant (2.5 vCPU, 5 GiB across the three apps) and 2 hours per session day for the live apps, less two months of free grant |
| Live online endpoint `nw-live-triage-<scope>`, blue and green | live target | Standard_DS3_v2 0.229 USD per hour; Azure ML adds no CPU surcharge (the surcharge meters are 0) | 16.03 USD: one instance 8 hours on 7 session days plus the second colour 2 hours per session; 247.32 if left running 45 days |
| Foundry resource, project, content filters, Agent Service basic setup | platform | no charge found in the price list for the resource or the project; tokens as above | 0 |
| Microsoft Purview (off), Defender for Containers (off) | optional | Purview: no Data Map capacity unit meter in eastus2 (not published); Defender for Containers 0.00941 USD per vCore-hour, 0.29 per image in the "Standard Images" meter | 0 |
| Delivery: Azure Pipelines or GitHub Actions | delivery | Azure Pipelines: one free Microsoft-hosted parallel job (not fetched today, see note); GitHub Actions free for public repositories | 0 |

Per tenant (26 including the instructor):

| Item | Unit price | Estimate |
| --- | --- | --- |
| Pipeline steps: 6 runs of 20 minutes on Standard_DS3_v2 (cluster or serverless) | 0.229 USD per hour | 0.46 USD |
| Tenant endpoints: triage and semantic on Standard_F2s_v2, 2 hours in each of two sessions | 0.0846 USD per hour | 0.68 USD |
| Search index and embeddings | inside the search unit; 0.02 per million embedding tokens | 0.02 USD |
| **Tenant rows** | | **1.16 USD** |

- Platform, delivery and live target add to **about 650 USD per cohort**, 26.00 per learner. Search S1 and API Management are 90 percent of it and bill every hour whether anyone works or not.
- With model spend the **per-tenant cost is about 4.30 USD** (1.16 plus 3.15).
- Cohort total: **about 760 USD** (650 plus 25 times 4.31).
- Cheaper variants: 14 tenants or fewer keep Search on Basic (saves 253.80); LiteLLM instead of API Management (`NW_GATEWAY_KIND=litellm`: PostgreSQL Burstable B1ms 0.017 USD per hour and 0.115 per GB-month of storage, about 6.50 for the cohort with the database stopped between sessions) saves about 215; API Management Developer saves 150.86 but is a classic tier with no SLA, where the Anthropic API gets a request rate limit instead of the token limit.

Credit to request per participant (cohort): platform share 26.00 plus per-tenant 4.31, 30.31 USD, **request 35 USD per learner**, on a pay-as-you-go subscription: Claude on Foundry cannot be deployed on free trial, student, sponsored credit-only or CSP subscriptions (Foundry Claude pages, 2026-09-22), so credits must land on a subscription with a payment method.

### Solo mode

As above in one resource group with one tenant; the learner deploys before a session and destroys after it (the deploy re-uploads the data and indexes; images are pushed again).

| Item | Cost |
| --- | --- |
| Platform per session day (8 hours): API Management 1.64, Search Basic 0.81, registry 0.17, the drill on the live endpoint 0.46, the tenant endpoints 0.34 | about 3.40 USD |
| Seven session days | 23.94 USD |
| Kept between sessions | nothing: destroy removes the group |
| Tenant rows | 1.16 USD |
| Model spend | 3.15 USD |
| **Solo cost for one learner** | **about 28 USD** |
| If the platform is left running for 45 days instead | about 350 USD (API Management 221.92, Search Basic 109.08, the rest as above) |
| **Daily idle cost of a deployed platform** (after `make stop-azure`: API Management, Search and the registry keep billing) | **solo 7.57 USD per day** (4.93 plus 2.42 plus 0.17 plus storage and the alert); **cohort 13.28 USD per day** with Search S1; about 2.80 per day solo with LiteLLM instead of API Management |
| **Residual after destroy** | **0 USD**: the resource group goes, the script purges the soft-deleted Key Vault, Foundry resource and API Management names, deletes the custom role definitions and sets Defender back to Free if it was turned on |

### Validation run budget, Azure

| Run | Cloud side | Model spend | Budget |
| --- | --- | --- | --- |
| Azure cohort mode: platform plus two tenants (Search Basic) | 4.91 platform day (API Management 1.64, Search 0.81, registry 0.17, live endpoint with the split 2.29) plus 2 times 1.16 | 2 times 3.15 | 20 USD |
| Azure solo mode | 4.91 plus 1.16 | 3.15 | 15 USD |

Prices not found on a vendor page (2026-09-29): Claude has no Azure price meter (Marketplace billing at Anthropic's rates, per the Learn billing page); mistral-small-2503 is Marketplace billed and has no inference meter either (trackers' figure used); gpt-oss-20b has no pay-per-token Foundry price (fine-tuning meters only, and no serverless deployment); Foundry hosted agents, file search and code interpreter show "$-"; workbooks and dashboards have no meter; Purview has no Data Map capacity unit meter in eastus2; Azure Pipelines' free parallel job was not fetched today (the price list carries no Azure DevOps meter in the query used).

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
