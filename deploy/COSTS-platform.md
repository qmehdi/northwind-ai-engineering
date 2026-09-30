# Cost sheet, managed platform (2026-09-29)

Replaces the archived `docs/archive/COSTS.md` (ADR 0008 to 0012, accepted). One environment per cloud track; the rows say which part of that environment each component is. Every number was fetched on the date shown from the vendor's pricing page or price list; nothing is estimated from memory. Two modes per cloud track (ADR 0009); the Local track has no cloud cost. Regions: us-east-1 and us-central1 unless the service is global.

Sources, all fetched 2026-09-29 unless a row says otherwise: AWS pricing pages (`aws.amazon.com/<service>/pricing`), the AWS price list files for SageMaker, CodeBuild and Bedrock in us-east-1 (`pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<offer>/current/us-east-1/index.csv`, published 2026-09-28) and the Bedrock foundation model feed the pricing page renders; Google Cloud pricing pages (`cloud.google.com/<product>/pricing`) fetched as HTML with curl because they render client side. Where a vendor page states no price, the cell says which page was checked.

Assumptions shared by every table:

- Cohort of 25 learners plus one instructor tenant, six parts plus Part 0, one part a week. The course has no time budget; the per-hour lines assume about two hours of platform use per part and scale with the hours your cohort actually spends.
- The platform lives 45 days (a Part 0 week plus six weekly sessions), 1,080 hours; a month is 730 hours.
- A session day is 8 hours of platform activity (pre-session checks, the session, clean-up); seven session days per cohort.
- Per-second and per-hour components that can be stopped are priced for session days only; the row says what they cost if left running for the 45 days.
- Everything else (registries, buckets, keys, alarms) is priced for the 45 days.

## Model spend, every track

| Role | AWS | Google Cloud | Local | Price per million tokens in and out (fetched 2026-09-29) |
| --- | --- | --- | --- | --- |
| Workhorse | gpt-oss-120b on Bedrock | gpt-oss-120b on the Agent Platform | gpt-oss-120b on Ollama | Bedrock us-east-1 0.15 and 0.60 (price list); Google 0.09 and 0.36 (generative AI pricing page) |
| Economy | Nova Micro | gpt-oss-120b with the Economy token caps (ADR 0010: gpt-oss-20b on Google's managed API retires on 2026-10-21) | gpt-oss-20b on Ollama | Nova Micro 0.035 and 0.14 (Bedrock price list, us-east-1); Google's Economy is the Workhorse model at 0.09 and 0.36; gpt-oss-20b for reference 0.07 and 0.30 on Bedrock us-east-1, 0.07 and 0.25 on Google until retirement |
| Judge | Claude Opus 5 on Bedrock (the calibrated judge) | Claude Opus 5 on the Agent Platform | Claude on either cloud, or judge-free | Bedrock us-east-1 in-region 5.50 and 27.50 (Bedrock foundation model feed; the feed carries no global row, the global endpoint is 5.00 and 25.00 per `docs/archive/COSTS.md`, 2026-09-08); Google global 5.00 and 25.00, regional 5.50 and 27.50 (generative AI pricing page) |

Per participant, recomputed on 2026-09-30 from what the guide tells each learner to run on the evaluation sets as they are now (111 policy cases with 66 in the gate split, 102 Judge calibration cases, 40 graded agent turns, 35 agent cases with the live gate at `--repeats 3`).

How the token sizes were measured (offline, no model called): the real harness code (`nw.policy.evaluate`, `nw.policy.calibrate`, `nw.agent.evaluate` with the `local` tool registry, so tool observations are the real ones) ran against a recording fake provider, and every request (system prompt, schema instructions, messages, tool specs) was counted with the Qwen2 tokenizer in `solution/.venv` (a 151k-token BPE, the nearest offline proxy for gpt-oss's o200k tokenizer):

| Call | Input tokens measured (mean) | Output priced at |
| --- | --- | --- |
| Policy answer, Workhorse (`answer`, 2,500-token context budget) | 1,789 (max 2,155) | 800, the call's `max_tokens` ceiling, which also covers gpt-oss's reasoning tokens (not measurable offline; the structured answer itself is about 65) |
| Policy faithfulness judgement, Judge | 454 | 166 output-equivalent tokens, see below |
| Judge calibration judgement (`judge_passage`) | 367 | 166 |
| Agent turn judgement, Judge | 340 (the 40 calibration turns: 345) | 166 |
| One agent run, Workhorse | first turn 1,198 (system, tool specs, ticket); mean turn 1,490, max 3,040 once tool observations (mean 412 tokens) are in | priced as 5 turns at 3,000 in and 1,024 out each (the loop's `max_tokens` ceiling); the scripted run needs 2.2 turns, a live model's turn count is not measured yet |

The Judge's 166 output-equivalent tokens come from the one measured Judge run: the calibration of Claude Opus 5 on Bedrock on 2026-09-27 cost 0.172 USD for 30 hand-written cases (`instructor/audit-2026-09-27.md`), whose inputs count 9,540 tokens here; at 5.00 and 25.00 that leaves 4,980 output tokens, 166 a judgement, and it absorbs any difference between Claude's tokenizer and this count. Unit prices per call, from the model table above:

| Unit | AWS | Google Cloud | Azure |
| --- | --- | --- | --- |
| Policy answer: 1,800 in, 800 out on the Workhorse | 0.00075 | 0.00045 | 0.00075 |
| One agent run: 5 x (3,000 in, 1,024 out) on the Workhorse | 0.0053 | 0.0032 | 0.0053 |
| Policy judgement: 450 in, 166 out on Opus 5 (AWS in-region 5.50 and 27.50, the others 5.00 and 25.00) | 0.0070 | 0.0064 | 0.0064 |
| Calibration judgement: 370 in, 166 out | 0.0066 | 0.0060 | 0.0060 |
| Agent turn judgement: 340 in, 166 out | 0.0064 | 0.0059 | 0.0059 |

What each learner runs, from the guide, in USD:

| Item (the command the guide names) | Calls | AWS | Google Cloud | Azure |
| --- | --- | --- | --- | --- |
| Pre-work and the first part's live check | under 1k tokens | 0.01 | 0.01 | 0.01 |
| Project 3, the no-judge baseline (`make eval-policy-baseline`) | 66 answers | 0.05 | 0.03 | 0.05 |
| Project 3, experiments on the dev split with `--no-judge`, three runs | 3 x 45 answers | 0.10 | 0.06 | 0.10 |
| Project 3, Judge calibration (`make calibrate-judge`), **solo only** | 102 judgements | 0.67 | 0.61 | 0.61 |
| Project 3, the judged baseline (`make eval-policy-baseline JUDGE=1`) | 66 answers, 44 judgements (the 22 must-refuse cases are refused and not judged; 66 at most, 0.51 on AWS) | 0.36 | 0.31 | 0.33 |
| Project 4, the first run (`nw.agent.evaluate --no-judge`) | 35 runs | 0.19 | 0.11 | 0.19 |
| Project 4, the framework port (AWS and Google Cloud only) | 17 runs | 0.09 | 0.05 | 0 |
| Project 4, turn Judge calibration (`make agent-calibrate-judge`), **solo only** | 40 judgements | 0.26 | 0.23 | 0.23 |
| Project 4, the live gate (`make agent-gate-live`, K=3) | 105 runs, 105 judgements | 1.23 | 0.95 | 1.17 |
| Project 4, the recorded run (`make agent-record`) | 35 runs, 35 judgements | 0.41 | 0.32 | 0.39 |
| Capstone: the loop and router comparison on 20 tickets (20 loop runs, 16 routed to the Workhorse, 2 to Economy, 2 P0s free) plus about 20 runs of demos and drills | 56 Workhorse runs, 2 Economy runs | 0.30 | 0.18 | 0.30 |
| Embeddings for the policy corpus and queries (Azure; the other tracks embed on the platform) | | | | 0.02 |
| Headroom for reruns, structured-output repairs and mistakes | | 1.00 | 1.00 | 1.00 |
| **Model spend per participant, solo** | | **4.67** | **3.87** | **4.41** |
| Less the two calibrations, run once by the instructor and shared (below) | | 0.93 | 0.85 | 0.85 |
| **Model spend per participant, cohort** | | **3.74** | **3.03** | **3.57** |

- The two calibrations measure the Judge model, not the learner's work, and the Judge id is the same for every tenant on a track (`DEFAULT_MODELS` in `nw/config.py`). In cohort mode the instructor runs `make calibrate-judge SHARE=1` and `make agent-calibrate-judge SHARE=1` once per track, which also write `data/golden/calibrations/policy-judge-<track>.json` and `agent-judge-<track>.json`; the instructor commits them upstream and learners sync their fork. `judge_calibration` (policy) and `judge_calibrated` (agent) read the learner's own report first and otherwise a committed report that measured the same Judge id, so the judged gates are gated for the whole room from one run: 0.93 USD on AWS, 0.85 on Google Cloud and Azure, once per cohort, on the instructor's tenant.
- The estimates that used to sit in the code and this sheet were sized for the Claude-era sets and a Claude Workhorse: "about 6 USD" for the judged run was the measured 6.08 of 2026-09-08 with Sonnet 5 answering. On gpt-oss-120b the judged run is about 0.36 USD, the no-judge run about 0.05, a live gate repeat about 0.41 on AWS (0.32 on Google Cloud, 0.39 on Azure).
- The Judge is the only Claude call: 2.14 of the 3.67 USD before headroom on AWS solo (58 percent), 1.21 of 2.74 in cohort mode.
- Not measured yet: a live model's agent turn count and gpt-oss's reasoning tokens (both priced at their ceilings, so the real figures should come in under these), and Claude's own token count for these prompts. Replace this table with the validation run's gateway spend.

## AWS track

AWS rows revised 2026-09-30 after the audit: the idle meters the first sheet missed (the gateway database held awake by LiteLLM's connections, public IPv4 addresses, Model Monitor jobs, the second live endpoint), and what the fixes added (the agents' egress network, CloudFront in front of the gateway, per-tenant secrets, the trail's own key). New prices fetched 2026-09-30 from the VPC, Route 53, Secrets Manager, Network Firewall and WAF pricing pages; the Aurora rate from the Aurora pricing page's published figures (0.12 USD per ACU-hour, Aurora Standard, us-east-1).

### Cohort mode: shared platform (deployed once per cohort by the instructor)

| Component | Part of the environment | Unit price (us-east-1, fetched 2026-09-29 unless marked) | Cohort estimate |
| --- | --- | --- | --- |
| SageMaker domain, managed MLflow tracking server | platform | domain: no charge for creating or configuring a Studio domain (SageMaker AI FAQ); tracking server Small 0.60 USD per hour, Medium 1.04, Large 1.91, metadata storage 0.10 USD per GB-month (price list) | 33.70 USD: Small server running 8 hours on each of 7 session days, 1 GB of metadata; 648 USD if left running 45 days |
| SageMaker Model Registry | platform | no registry line on the SageMaker AI pricing page or FAQ | 0 |
| ECR, artifacts bucket, KMS | platform | ECR 0.10 USD per GB-month (500 MB free for one year); S3 Standard 0.023 USD per GB-month, PUT 0.005 and GET 0.0004 USD per 1,000; KMS 1.00 USD per key per month prorated hourly, 0.03 USD per 10,000 requests after 20,000 free | 7.85 USD: 10 GB of images, 50 GB of artifacts and pipeline outputs, 3 keys (platform, trail, CDK), 1.5 months; lifecycle rules expire capture, traces and monitoring output after 90 days |
| Model gateway (multi-provider gateway guidance, LiteLLM on ECS Fargate, load balancer) | platform | Fargate Linux ARM 0.0324 USD per vCPU-hour and 0.00356 per GB-hour (x86 0.0404 and 0.00444); Application Load Balancer 0.0225 USD per hour plus 0.008 per LCU-hour | 54.30 USD: one 0.5 vCPU 1 GB task (0.0198 USD per hour) plus the load balancer at about 1 LCU (0.0305), 0.0503 USD per hour, 1.21 per day, 1,080 hours |
| Gateway keys database (Aurora Serverless v2) | platform | 0.12 USD per ACU-hour (fetched 2026-09-30), 0.5 ACU whenever awake, 0 when paused; storage 0.10 USD per GB-month. The 0 ACU floor only pauses after 15 idle minutes, and LiteLLM holds connections open, so the database is awake whenever the gateway task runs | 64.95 USD: 0.5 ACU for 1,080 hours (64.80) and 1 GB; 0.48 per session day if the gateway is stopped between sessions (`make stop-aws` scales the task to zero and the database pauses) |
| HTTPS front for the gateway (CloudFront) | platform | CloudFront pay-as-you-go always-free allowance of 1 TB out and 10 million requests a month (carried from the CloudFront pay-as-you-go page, not refetched 2026-09-30) | 0: a cohort's gateway traffic is a few GB and well under a million requests |
| Agent egress control (NAT gateway, DNS Firewall, S3 gateway endpoint) | platform | NAT gateway 0.045 USD per hour and 0.045 per GB processed; DNS Firewall 0.0005 USD per domain per month and 0.60 per million queries; S3 gateway endpoint no charge (fetched 2026-09-30) | 49.30 USD: one NAT gateway 1,080 hours (48.60), 5 GB processed (0.23), DNS Firewall queries (0.60); `NW_AGENT_EGRESS=public` removes the row |
| Public IPv4 addresses | platform | 0.005 USD per hour per address, in use or idle (fetched 2026-09-30) | 21.60 USD: 4 addresses (the NAT gateway, the gateway load balancer in two subnets, the gateway task) for 1,080 hours |
| Secrets Manager | platform and tenant | 0.40 USD per secret per month, 0.05 per 10,000 calls (fetched 2026-09-30) | 34.80 USD: 58 secrets for 1.5 months (the service API key, the gateway master, salt and origin keys, the database password, the live gateway key, and per tenant a gateway key and an API key) |
| AgentCore Gateway, Agent Registry, Identity | platform | Gateway 0.005 USD per 1,000 invocations, search 0.025 per 1,000, 0.02 USD per 100 tools indexed per month; Agent Registry (general availability since 2026-08-31, no longer preview) first 5,000 records, 1 million search and 2 million list or get calls free per month, then 0.40 USD per 1,000 records, 0.02 per 1,000 search, 0.004 per 1,000 list or get; Identity 0.010 USD per 1,000 tokens or keys, free through Runtime or Gateway | 0.10 USD: 12,500 tool invocations, 7 tools, 26 registry records |
| CloudWatch cross-account observability, CloudTrail | platform | cross-account observability at no additional cost; custom metrics 0.30 USD per metric-month for the first 10,000, prorated by the hour and charged only when written; alarms 0.10 USD per alarm-metric per month after 10 free; logs 0.50 USD per GB ingested after 5 GB, 0.03 per GB-month stored; CloudTrail first management trail free, 2.00 USD per 100,000 events after | 26.00 USD: 806 metric series (31 per tenant) written about 22 hours, 100 billable alarm-metrics for 1.5 months, 10 GB of logs |
| Cognito user pool | platform | 10,000 monthly active users free on Lite and Essentials, then 0.0055 (Lite) or 0.015 (Essentials) USD per MAU; Plus 0.020 with no free tier | 0: 26 users |
| SageMaker Pipelines runs (processing, training, evaluation) per tenant | tenant | Pipelines: no additional charge, pay for the instances (SageMaker AI FAQ); ml.m5.large 0.115 USD per hour and ml.m5.xlarge 0.23 for Processing, Training and Hosting (price list) | 0.70 USD per tenant: 8 runs at 0.087 USD (processing 10 minutes on ml.m5.large, training 15 minutes on ml.m5.xlarge, evaluation 5 minutes on ml.m5.large), 3 for Project 1, 2 for Project 2, 3 reruns |
| SageMaker Serverless Inference per tenant | tenant | 0.00002 USD per GB-second, 0.00004 USD per second at 2 GB (price list, memory sizes 1 to 6 GB); no per-request line in the price list; zero when idle | 0.20 USD per tenant: two endpoints (triage, semantic) at 2 GB, 250 seconds per session and 1,000 seconds of cold starts each |
| Knowledge Base on S3 Vectors per tenant | tenant | S3 Vectors 0.06 USD per GB-month, PUT 0.20 USD per GB, queries 2.50 USD per million plus 0.004 USD per TB processed under 100,000 vectors and 0.01 USD per GB returned; self-managed Knowledge Bases list no retrieval charge (the managed Knowledge Base tier is 5.00 USD per GB-month of index and 1.00 USD per 1,000 Retrieve calls and is not used) | under 0.01 USD per tenant: 212 chunks, 1,000 queries |
| Bedrock Prompt Management | tenant | no Prompt Management line on the Bedrock pricing page (Prompt Optimization, not used, is 0.03 USD per 1,000 tokens); only the inference of the model chosen when testing a prompt | 0 |
| AgentCore Runtime, Memory, Observability, Evaluations per tenant | tenant | Runtime v1 0.0895 USD per vCPU-hour and 0.00945 per GB-hour, per second while active (v2 consumption 0.1276 and 0.0169, committed baseline 0.0997 and 0.0132 from October 2026); Memory 0.25 USD per 1,000 new events, 0.75 per 1,000 records stored per month, 0.50 per 1,000 retrievals; Observability at CloudWatch rates; Evaluations built-in 0.0024 USD per 1,000 input and 0.012 per 1,000 output tokens, custom 1.50 USD per 1,000 evaluations | 1.61 USD per tenant: 2 active hours at 1 vCPU 2 GB (0.22), 500 events and 50 records (0.17), 50 MB of spans (0.02), 100 built-in evaluations at 4k in and 0.2k out (1.20) |
| CodePipeline, CodeBuild, CodeDeploy | delivery | CodePipeline V2 0.002 USD per action-minute after 100 free per month (V1 1.00 USD per active pipeline after one free); CodeBuild arm1.small 0.0034 USD per minute, general1.small 0.005, 100 free minutes per month; CodeDeploy no charge for Lambda, ECS and EC2 deployments, 0.02 USD per on-premises instance update (FAQ); CodeConnections: no price published, `aws.amazon.com/codeconnections/pricing` returns 404 and the CodePipeline pricing page bills the connection only as source action minutes | 5.40 USD: 26 tenants, 4 runs each of 15 action-minutes and 8 arm1.small build-minutes |
| Real-time SageMaker endpoints with the blue/green canary | live target | ml.m5.large 0.115 USD per hour per instance; the canary runs a second fleet for its duration | 15.60 USD: two live endpoints (triage and semantic) 8 hours on 7 session days plus 2 canary hours per session each; 248 USD if both are left running 45 days |
| Model Monitor data quality jobs | live target | processing on ml.m5.large 0.115 USD per hour, billed per second; one job an hour per live endpoint while it exists | 2.15 USD: 2 endpoints, 8 jobs a session day each, 7 days, about 10 minutes a job (0.019 USD); 41.40 USD if both endpoints are left running 45 days |
| AgentCore Runtime, guardrail, policy engine | live target | Guardrails per 1,000 text units: content filters 0.15 USD, denied topics 0.15, sensitive information 0.10, contextual grounding 0.10 (prompt attack through InvokeGuardrailChecks 0.08); Policy 0.000025 USD per authorization, authoring 0.13 USD per 1,000 tokens; Runtime as above | 4.40 USD: 5,000 guarded requests at 2 text units through content filters, PII and grounding (3.50), 5,000 authorizations (0.13), 7 demo hours of runtime (0.76) |
| Budgets and alarms | platform | budget monitoring and notifications free of charge; action-enabled budgets 0.10 USD per day after the first two (the platform has one, with the stop action at 100 percent); alarms priced in the CloudWatch row | 0 |
| Priced, not deployed: AWS WAF on the gateway distribution; AWS Network Firewall for domain-aware (SNI) egress | platform | WAF 5.00 USD per web ACL per month, 1.00 per rule per month, 0.60 per million requests; Network Firewall 0.395 USD per endpoint-hour and 0.065 per GB processed (fetched 2026-09-30) | WAF with 5 rules: 15.00 USD; Network Firewall with one endpoint: 426.60 USD for 1,080 hours, the reason the course stops at the DNS allow-list |

- Platform, delivery and live target rows add to **about 320 USD per cohort**, 12.80 per learner (138 in the first sheet plus 182 for the rows added on 2026-09-30).
- Tenant rows add to **2.52 USD per tenant**; with the cohort model spend (3.74) the **per-tenant cost is about 6.26 USD**.
- Cohort total: about 477 USD (320 plus 25 times 6.26, plus 0.93 for the two Judge calibrations the instructor runs once and shares).
- The biggest idle meters are now the gateway database (awake while the task runs), the NAT gateway and the MLflow server; stopping the platform between sessions (`make stop-aws`) takes the first and the last to zero, and `NW_AGENT_EGRESS=public` removes the NAT gateway at the price of the egress control.

Credit to request per participant (cohort): platform share 12.80 plus per-tenant 6.26, 19.06 USD, **request 20 USD per learner** on the instructor's account; set the cohort budget alarm at 500 USD.

### Solo mode

Platform plus one tenant in one account; the learner deploys before a session and destroys after it.

| Item | Cost |
| --- | --- |
| Platform per session day (8 hours): MLflow Small 4.80, gateway task and load balancer 0.40, gateway database 0.48, two live endpoints plus canary 2.07, Model Monitor 0.31, NAT gateway 0.36, public IPv4 0.16, guardrail 0.14, KMS 0.03, delivery run inside the free tiers | about 8.75 USD |
| Seven session days | 61.25 USD |
| Kept between sessions for 45 days: 4 GB in ECR, 2 GB in S3, 3 KMS keys | 5.20 USD |
| Tenant rows | 2.52 USD |
| Model spend, solo (both calibrations included) | 4.67 USD |
| **Solo cost for one learner** | **about 74 USD** (61.25 plus 5.20 plus 2.52 plus 4.67) |
| If the platform is left running for 45 days instead (MLflow 648, gateway 54, database 65, endpoints 248, Model Monitor 41, NAT gateway 49, public IPv4 22, keys and secrets 9) | about 1,136 USD |
| **Daily idle cost of a deployed platform**: MLflow Small 14.40, gateway task and load balancer 1.21, gateway database 1.44, two live endpoints 5.52, Model Monitor 0.92, NAT gateway 1.08, 4 public IPv4 0.48, 100 alarms 0.33, secrets 0.11, KMS 0.10, ECR and S3 0.07; Serverless Inference and AgentCore bill nothing idle | **25.66 USD per day**; 2.78 per day with the tracking server stopped, the live endpoints deleted and the gateway scaled to zero (`make stop-aws`: the database pauses, the load balancer, NAT gateway and three addresses remain) |
| **Residual after destroy**: retained artifacts bucket (50 GB, 1.15 per month), retained log groups (10 GB, 0.30 per month), ECR if retained (1.00 per month); KMS keys in their deletion window at most 1.00 per key per month (the KMS page does not say whether pending keys bill) | **about 2.50 USD per month**, plus at most 2.00 for the key deletion window |

## Google Cloud track

### Cohort mode

| Component | Part of the environment | Unit price (us-central1, fetched 2026-09-29) | Cohort estimate |
| --- | --- | --- | --- |
| Artifact Registry, Cloud Storage, KMS | platform | Artifact Registry 0.000136986 USD per GiB-hour (0.10 per GB-month) after 0.5 GB free; Cloud Storage Standard 0.000027397 USD per GiB-hour (0.020 per GB-month), Class A 0.005 and Class B 0.0004 USD per 1,000, 5 GB-months and 5,000 Class A free; Cloud KMS 0.06 USD per active software key version per month (0.000082192 per hour), 0.03 USD per 10,000 operations after 10,000 free | 3.00 USD: 10 GB of images, 50 GB of artifacts, 2 key versions, 1.5 months |
| Model gateway (LiteLLM on Cloud Run) | platform | instance-based billing 0.000018 USD per vCPU-second and 0.000002 per GiB-second, free tier 240,000 vCPU-seconds and 450,000 GiB-seconds per month (`docs/archive/COSTS.md`, 2026-09-08); request-based rates in the same file | 70.00 USD: one always-warm instance of 1 vCPU 1 GiB (0.072 USD per hour, 1.73 per day) for 1,080 hours, minus the free tier |
| Cloud Monitoring, Logging, Trace, Security Command Center | platform | Monitoring 0.2580 USD per MiB after 150 MiB per billing account; Logging 0.50 USD per GiB after 50 GiB per project, retention 0.01 USD per GiB-month beyond 30 days; Trace 0.20 USD per million spans after 2.5 million; alerting policies 0.35 USD per month per metric reference effective 2027-09-01; Security Command Center Standard tier free of charge | 0.10 USD: 520 log-based metrics under 150 MiB, logs (Data Access audit logs for Secret Manager and the Agent Platform included, and their copy in the 400 day audit bucket) under 50 GiB, spans under 2.5 million; the audit bucket's retention beyond 30 days is about 2 GiB at 0.01 per GiB-month |
| Vertex AI Pipelines runs per tenant | tenant | 0.03 USD per pipeline run plus the compute of each step; custom training e2-standard-4 0.154126276 USD per hour (the smallest E2 in the training table) | 0.86 USD per tenant: 8 runs at 0.107 USD (run fee plus 30 step-minutes on e2-standard-4) |
| Vertex AI Experiments, Model Registry | tenant | Experiments: no charge line; ML Metadata 10 USD per GiB-month prorated; TensorBoard 10 USD per GiB-month if used; Model Registry: no cost for models in the registry, cost only on deployment | under 0.01 USD per tenant |
| Container serving from the registry artifact on Cloud Run per tenant | tenant | request-based billing (`docs/archive/COSTS.md`, 2026-09-08): 0.000024 USD per vCPU-second and 0.0000025 per GiB-second while serving, 2 million requests and 180,000 vCPU-seconds free per month | 0.50 USD per tenant: 6 sessions at 0.15 before the free tier |
| RAG Engine on RagManagedDb (the default, `rag_backend = "managed"`), a corpus per tenant | platform, project-wide tier | RAG Engine itself has no price (RAG Engine billing page: ingestion, default parser and fixed-size chunking free; you pay the embedding model, the vector database and any reranker); the RagManagedDb Basic tier is a Spanner Enterprise instance of 100 processing units with backup, Spanner Enterprise regional 1.23 USD per node-hour (1,000 units), so 0.123 USD per hour, billed every hour from the first corpus until the tier is set to Unprovisioned (`make destroy-gcp` does it) | 132.84 USD for the cohort: 1,080 hours at 0.123, whatever the number of tenants; 2.95 per day idle |
| Alternative: RAG Engine on Vector Search (`rag_backend = "vector_search"`) | tenant, shared endpoint | Vector Search e2-standard-2 0.0938084 USD per node-hour, index build 3.00 USD per GiB, streaming updates 0.45 USD per GiB | 101.30 USD for the cohort instead of the RagManagedDb row: one index endpoint on one e2-standard-2 node for 1,080 hours, corpora per tenant on it (4.05 per tenant); a node per tenant would be 2,533 USD |
| Gateway database (Cloud SQL for PostgreSQL, db-f1-micro, 10 GB SSD, zonal, no backups) | platform | db-f1-micro 0.0105 USD per hour, SSD storage 0.000232877 USD per GiB-hour (Cloud SQL pricing page, fetched 2026-09-30); billed every second the instance runs (activation policy ALWAYS), storage also while stopped | 13.86 USD: always on for 1,080 hours (0.0128 per hour, 0.31 per day); 0.06 per day while `make stop-gcp` holds it stopped |
| Agent Engine runtime, sessions, Memory Bank per tenant | tenant | 0.0864 USD per vCPU-hour, 0.0090 per GB-hour, free tier 50 vCPU-hours and 100 GB-hours per month, 0.25 USD per 1,000 session events (2026-09-08) | 0.13 USD per tenant: 2 active hours at 1 vCPU 2 GB (0.21, inside the project free tier for the cohort) and 500 events (0.13) |
| Cloud Build, Cloud Deploy | delivery | Cloud Build e2-standard-2 0.006 USD per build-minute, e2-medium 0.003, 2,500 free build-minutes per billing account per month; Cloud Deploy: first active multiple-target delivery pipeline free, 5.00 USD per additional active multiple-target pipeline per month, single-target pipelines not charged | 0: 832 build-minutes, one single-target pipeline |
| Vertex endpoint with traffic split | live target | e2-standard-2 0.0770564 USD per node-hour, n1-standard-2 0.1095 (online prediction table); the split adds a node | 5.25 USD: one shared e2-standard-2 endpoint 8 hours on 7 session days plus 2 split hours per session; 83 USD if left running 45 days |
| Model Armor | live target | 0.10 USD per million tokens, first 2 million per month free (2026-09-08) | 0.30 USD: 5,000 requests at 1,000 tokens |
| Budgets and alerting | platform | Cloud Billing budgets carry no price on the Monitoring pricing page; alert policies free until 2027-09-01 | 0 |

- Platform, delivery, live target, the RagManagedDb tier and the gateway database add to **about 225 USD per cohort** (3.00 plus 70.00 plus 132.84 plus 13.86 plus 5.25 plus 0.30 plus 0.10), 9.00 per learner for 25 learners. The two always-on meters are the RagManagedDb tier (59 percent of the platform) and Cloud SQL; `rag_backend = "vector_search"` is about 31 USD cheaper for a cohort; RAG Engine's serverless mode (public preview, RAG Engine overview page) would remove the tier once it is generally available.
- Tenant rows add to **1.50 USD per tenant**; with the cohort model spend (3.03) the **per-tenant cost is about 4.53 USD**.
- Cohort total: about 339 USD (225 plus 25 times 4.53, plus 0.85 for the two shared Judge calibrations).

Credit to request per participant (cohort): platform share 9.00 plus per-tenant 4.53, 13.53 USD, **request 15 USD per learner**. New accounts carry 300 USD of Google credits for 90 days (fetched 2026-09-29), which covers solo mode for one learner even with the platform left running.

### Solo mode

As above in one project; the learner deploys before a session and destroys after it.

| Item | Cost |
| --- | --- |
| Platform per session day (8 hours): gateway 0.58, RagManagedDb Basic tier 0.98, Cloud SQL 0.10, endpoint plus split 0.77 | about 2.43 USD |
| Seven session days | 17.01 USD |
| Kept between sessions for 45 days: 4 GB in Artifact Registry, 2 GB in Cloud Storage, 2 key versions, less the free tiers | 0.55 USD |
| Tenant rows | 1.50 USD |
| Model spend, solo (both calibrations included) | 3.87 USD |
| **Solo cost for one learner** | **about 23 USD** (17.01 plus 0.55 plus 1.50 plus 3.87) |
| If the platform is left running for 45 days instead, the live endpoint empty (gateway 77.85, RagManagedDb 132.84, Cloud SQL 13.86, storage and registry about 5) | about 230 USD, inside the 300 USD credit |
| **Daily idle cost of a deployed platform**: gateway 1.73, RagManagedDb Basic tier 2.95, Cloud SQL 0.31, endpoint 1.85, storage 0.06 | **6.90 USD per day**; 4.80 per day after `make stop-gcp` (endpoint undeployed, Cloud SQL stopped); the RagManagedDb tier keeps billing until destroy sets it to Unprovisioned, because Unprovisioned deletes every corpus |
| **Residual after destroy**: retained bucket (50 GB, 1.00 per month) and registry (10 GB, 0.95 per month), the Terraform state bucket (under 0.01); logs inside the free allotment. If the RagManagedDb tier were left provisioned (`rag_unprovision_on_destroy = false`), add 88.56 per month | **about 2 USD per month** |

## Azure track

Region eastus2 (the default of `deploy/azure`), prices fetched 2026-09-29. Sources: the Azure Retail Prices API (`https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&currencyCode=USD`, filtered on `armRegionName eq 'eastus2'` and the service name, pay-as-you-go consumption rows; no key needed) and, for what the API does not carry, the pricing pages `azure.microsoft.com/en-us/pricing/details/<service>/` (container-apps, monitor, cost-management, cognitive-services/content-safety, foundry-agent-service, ai-foundry-models/aoai) and Microsoft Learn's Claude billing page (`learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models-billing`, updated 2026-09-11). The Foundry models pricing page renders "$-" for every price; the API rows are the ones used. Same assumptions as the other tracks: 25 learners plus one instructor tenant, 45 days (1,080 hours), seven 8-hour session days.

### Model spend

| Role | Deployment (`deploy/azure/main.bicep`, `models`) | Price per million tokens in and out, Global Standard |
| --- | --- | --- |
| Workhorse | `gpt-oss-120b` (format OpenAI-OSS, version 1) | 0.15 and 0.60 (Foundry Models, API) |
| Economy | `mistral-small-2503` (format Mistral AI, version 1) | 0.10 and 0.30: sold through Azure Marketplace, so the Retail Prices API has no inference row; the rate is the Foundry catalogue price as third-party trackers record it (futureagi.com, getmaxim.ai, verified 2026-08-06), confirm in the portal's Pricing tab in the delivery week. Rejected, all in eastus2 Global Standard: gpt-5.4-nano 0.20 and 1.25 (API), dearer than the Workhorse; gpt-5-nano 0.05 and 0.40 (API) retires 2027-02-09; gpt-4.1-nano retires 2026-10-14; Phi-4-mini-instruct 0.075 and 0.30 (API) has no tool calling; Ministral-3B 0.04 and 0.04 (trackers) calls tools but is a 3B model for a multi-step agent loop |
| Judge | `claude-opus-5` (format Anthropic, version 2) | 5.00 and 25.00, cache reads 0.50 and 5-minute cache writes 6.25: Claude has no Azure price meter; it is billed through Azure Marketplace in Claude Consumption Units of 0.01 USD at Anthropic's list rates (Learn billing page); US Data Zone deployments cost 1.1 times |
| Embeddings | `text-embedding-3-small` | 0.02 per million (API, `text-embedding-3-small-glbl`) |

Per participant, the Azure column of the per-participant table above (Workhorse at 0.15 and 0.60, Judge at 5.00 and 25.00, no framework port on this track, embeddings 0.02): **model spend 3.57 USD in cohort mode, 4.41 USD solo** with the two Judge calibrations. The Economy role is cheaper than the Workhorse on both input and output, which is the point of the cheap-first router; `models` in `main.bicep` and `NW_MODEL_ECONOMY` swap it in one line each.

Content filters with Prompt Shields on the deployments carry no separate meter in the price list; the standalone Content Safety API would be 0.375 USD per 1,000 text records after 5,000 free a month, Prompt Shields included (Content Safety page). The Foundry Agent Service states "no additional charge for creating or running Foundry-native agents using prompts and workflows" (Agent Service page); hosted agents, file search and code interpreter show "$-" (not published). Foundry evaluations have no price of their own; the Azure ML evaluation token meters are 0.02 USD per 1,000 input and 0.06 per 1,000 output tokens (API), and the course's judged evaluations run on the Judge deployment, counted above.

### Cohort mode: shared platform (deployed once per cohort by the instructor)

| Component | Part of the environment | Unit price (eastus2, fetched 2026-09-29) | Cohort estimate |
| --- | --- | --- | --- |
| API Management Basic v2, the AI gateway | platform | 0.20548 USD per hour, 10 million calls included, then 0.03 per 10,000; the "AI Gateway Requests" meter is 0; Developer 0.0658 per hour, Standard v2 0.9589; Consumption (0.035 per 10,000 after 1 million free) has no llm-token-limit | 221.92 USD: 1,080 hours; the tier cannot be paused |
| Azure AI Search | platform | Basic 0.101 USD per search unit hour (15 indexes), Standard S1 0.336 (50 indexes); semantic ranker free plan | 362.88 USD: S1, because 27 owners need 27 indexes; 109.08 on Basic for 14 tenants or fewer |
| Container Registry | platform | Basic 0.1666 USD per day, Standard 0.6666; storage 0.10 USD per GB-month | 10.50 USD: 45 days plus 20 GB of images for 1.5 months, before any storage the tier includes |
| Storage: the lake (hierarchical namespace, Hot LRS) and the workspace account (flat, Hot LRS) | platform | lake 0.018 USD per GB-month, writes 0.065 and reads 0.005 per 10,000; flat 0.0184 per GB-month, writes 0.05 and reads 0.004 per 10,000 | 7.30 USD: 50 GB in the lake and 30 GB in the workspace account for 1.5 months, 700,000 writes, 3 million reads |
| Key Vault Standard | platform | 0.03 USD per 10,000 operations | 0.90 USD: 300,000 secret reads (every app start reads three: its owner's API key, its gateway key, the Application Insights string) |
| Log Analytics and Application Insights (workspace based) | platform | 2.76 USD per GB ingested after 5 GB free per billing account per month; 31 days retention included | 20.70 USD: 15 GB over the cohort (the workspace caps at 1 GB a day), 7.5 GB inside the free allowance |
| Alerts, action group, workbook, budget | platform | metric alerts: first 10 time series free, then 0.10 USD each per month; log search alert at 15 minutes 0.50 per month; email notifications first 1,000 free; workbooks: no meter and no price on the Monitor page (not published); budgets free (Cost Management page) | 0.75 USD: seven metric alerts (5xx and p95 on both live endpoints, 5xx on the three live apps) inside the free series, one drift log alert |
| Container Apps (consumption) for every owner's policy, agent and MCP apps | platform and tenant | 0.000024 USD per active vCPU-second, 0.000003 per GiB-second, 0.40 per million requests; 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests free per subscription per month (Container Apps page); scale to zero, no environment fee on the consumption plan | 5.83 USD: 2 active hours per tenant (2.5 vCPU, 5 GiB across the three apps) and 2 hours per session day for the live apps, less two months of free grant |
| Live online endpoint `nw-live-triage-<scope>`, blue and green | live target | Standard_DS3_v2 0.229 USD per hour; Azure ML adds no CPU surcharge (the surcharge meters are 0) | 16.03 USD: one instance 8 hours on 7 session days plus the second colour 2 hours per session; 247.32 if left running 45 days |
| Foundry resource, project, content filters, Agent Service basic setup | platform | no charge found in the price list for the resource or the project; tokens as above | 0 |
| EU Foundry resource (`foundry-eu`, swedencentral) with `Mistral-Large-3` on Data Zone Standard | platform | no charge for the resource; tokens at the Mistral Large 3 Marketplace rate (not in the Retail Prices API; confirm in the portal's Pricing tab in the delivery week) | 0 idle; EU calls are the 67 EU accounts' share of the model spend above |
| Network: virtual network, the apps subnet's security group (egress limited to Azure), the allowed-sizes Azure Policy, storage lifecycle rules | platform | no meter for a virtual network, a network security group, a policy assignment or a lifecycle rule (lifecycle deletes are free); fetched 2026-09-30 | 0 |
| Private DNS zone for the LiteLLM database (`NW_GATEWAY_KIND=litellm` only) | platform | 0.50 USD per zone per month (first 25), 0.40 per million queries (Azure DNS, Retail Prices API 2026-09-30) | 0.75 USD for 1.5 months |
| Egress by name: Azure Firewall with a route table on the apps subnet (priced, not deployed) | organisation | Basic 0.395 USD per hour plus 0.065 per GB processed; Standard 1.25 per hour plus 0.016 per GB (eastus2, Retail Prices API 2026-09-30) | not deployed: 426.60 USD for 1,080 hours on Basic; the course limits egress to Azure service tags instead (`deploy/azure/README.md`, "Identity and security notes") |
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
- With the cohort model spend the **per-tenant cost is about 4.73 USD** (1.16 plus 3.57).
- Cohort total: **about 769 USD** (650 plus 25 times 4.73, plus 0.85 for the two shared Judge calibrations).
- Model spend has a ceiling per tenant: API Management's `llm-token-limit` carries a monthly `token-quota` of 6 million tokens per subscription (`tenantTokensPerMonth`, `NW_TENANT_TOKENS_PER_MONTH`) next to the 20,000 a minute. The whole course priced at its ceilings is about 5.4 million tokens a learner (`make agent-gate-live` alone, 105 runs of about 20,000 tokens at 5 turns of 3,000 in and 1,024 out, is 2.1 million; at the measured turn size and 2.2 turns a run it is about 0.4 million), so 6 million leaves headroom until the validation run measures the real figure. On the Workhorse 6 million tokens are at most 3.60 USD a month per tenant (all of it output at 0.60 per million); the same tokens on the Judge would be 30 to 150 USD, so lower the quota for a cohort that routes heavy Judge use through its own keys. A tenant past the quota gets a 403 until the month turns. LiteLLM mode keeps its per-key USD budget (`NW_TENANT_BUDGET_USD`).
- Cheaper variants: 14 tenants or fewer keep Search on Basic (saves 253.80); LiteLLM instead of API Management (`NW_GATEWAY_KIND=litellm`: PostgreSQL Burstable B1ms 0.017 USD per hour and 0.115 per GB-month of storage, about 6.50 for the cohort with the database stopped between sessions) saves about 215; API Management Developer saves 150.86 but is a classic tier with no SLA, where the Anthropic API gets a request rate limit instead of the token limit.

Credit to request per participant (cohort): platform share 26.00 plus per-tenant 4.73, 30.73 USD, **request 35 USD per learner**, on a pay-as-you-go subscription: Claude on Foundry cannot be deployed on free trial, student, sponsored credit-only or CSP subscriptions (Foundry Claude pages, 2026-09-22), so credits must land on a subscription with a payment method.

### Solo mode

As above in one resource group with one tenant; the learner deploys before a session and destroys after it (the deploy re-uploads the data and indexes; images are pushed again).

| Item | Cost |
| --- | --- |
| Platform per session day (8 hours): API Management 1.64, Search Basic 0.81, registry 0.17, the drill on the live endpoint 0.46, the tenant endpoints 0.34 | about 3.40 USD |
| Seven session days | 23.94 USD |
| Kept between sessions | nothing: destroy removes the group |
| Tenant rows | 1.16 USD |
| Model spend, solo (both calibrations included) | 4.41 USD |
| **Solo cost for one learner** | **about 30 USD** (23.94 plus 1.16 plus 4.41) |
| If the platform is left running for 45 days instead | about 350 USD (API Management 221.92, Search Basic 109.08, the rest as above) |
| **Daily idle cost of a deployed platform** (after `make stop-azure`: API Management, Search and the registry keep billing) | **solo 7.57 USD per day** (4.93 plus 2.42 plus 0.17 plus storage and the alert); **cohort 13.28 USD per day** with Search S1; about 2.80 per day solo with LiteLLM instead of API Management |
| **Residual after destroy** | **0 USD**: the Azure ML workspace is deleted permanently first (a group delete would only soft-delete it and hold its name 14 days), then the resource group goes; the script purges the soft-deleted Key Vault, Foundry resource and API Management names, deletes the custom role definitions and the allowed-sizes policy definition (subscription scope) and sets Defender back to Free if it was turned on |

### Validation run budget, Azure

| Run | Cloud side | Model spend | Budget |
| --- | --- | --- | --- |
| Azure cohort mode: platform plus two tenants (Search Basic) | 4.91 platform day (API Management 1.64, Search 0.81, registry 0.17, live endpoint with the split 2.29) plus 2 times 1.16 | 2 times 3.57 plus 0.85 for the calibrations | 20 USD |
| Azure solo mode | 4.91 plus 1.16 | 4.41 | 15 USD |

Prices not found on a vendor page (2026-09-29): Claude has no Azure price meter (Marketplace billing at Anthropic's rates, per the Learn billing page); mistral-small-2503 is Marketplace billed and has no inference meter either (trackers' figure used); gpt-oss-20b has no pay-per-token Foundry price (fine-tuning meters only, and no serverless deployment); Foundry hosted agents, file search and code interpreter show "$-"; workbooks and dashboards have no meter; Purview has no Data Map capacity unit meter in eastus2; Azure Pipelines' free parallel job was not fetched today (the price list carries no Azure DevOps meter in the query used).

## Local track

No cloud cost. Requirements, measured 2026-09-30: a Mac with 32 GB for gpt-oss:20b in native Ollama beside Docker at 16 GB, or 16 GB with `SMALL=1` (qwen3:4b); about 60 GB of Docker disk for the images, volumes and build cache (prune the cache after rebuilds); the containers use about 5.3 GB at idle. gpt-oss:120b needs a GPU (the `gpu` profile).

## Validation run budget

One deploy-to-destroy per cloud track and mode, four runs, each on one day with the platform up 8 hours; waits for the go-ahead per track.

| Run | Cloud side | Model spend | Budget |
| --- | --- | --- | --- |
| AWS cohort mode: platform plus two tenants | 8.95 platform day (the solo day plus 0.20 for the two tenants' secrets and profiles) plus 2 times 2.52 tenant rows | 2 times 3.74 plus 0.93 for the calibrations | 25 USD |
| AWS solo mode: platform plus one tenant | 8.75 plus 2.52 | 4.67 | 20 USD |
| Google Cloud cohort mode: platform plus two tenants | 2.43 platform day plus 2 times 1.50 | 2 times 3.03 plus 0.85 for the calibrations | 15 USD |
| Google Cloud solo mode | 2.43 plus 1.50 | 3.87 | 10 USD |
| **Total** | | | **70 USD**, budget alarm at 100 USD on each account; the Google runs fit in a new account's 300 USD credit |

Prices not found on a vendor page (2026-09-29): CodeConnections (pricing URL returns 404, no charge stated anywhere on aws.amazon.com); SageMaker Serverless Inference has no per-request price in the price list, only per second by memory size; the Bedrock feed has no global-endpoint row for Claude Opus 5 (in-region 5.50 and 27.50 fetched, global 5.00 and 25.00 carried from 2026-09-08); Cloud Run and Agent Engine rates are carried from 2026-09-08.
