# Service level objectives

What the platform promises, per service, and what measures each promise. Every number below already exists somewhere in this repo: an alarm threshold in `deploy/aws/stacks/areas/serving.py` or `deploy/gcp/modules/observability/main.tf`, a Grafana alert in `deploy/local/grafana`, a gate bar in `nw/triage/promote.py`, `nw/semantic/promote.py` or `nw/agent/evaluate.py`, a drift bar in `nw/*/monitor.py`, a measurement the guide asks for, or a line in `COSTS.md`. Nothing has been measured in production yet; the validation run and the first month of traffic replace the targets that say so.

## SLIs and SLOs

| SLI | Service | SLO | Measured by | Where the number comes from |
| --- | --- | --- | --- | --- |
| Availability: share of requests answered without a 5xx | every service | No five-minute window with more than five 5xx responses. At the capstone's rate (about 40 requests in five minutes) that is 87.5 percent of requests; at one probe every two seconds it is 96.7 percent. Set a percentage after the first month of real traffic | AWS: the live endpoints' `<endpoint>-5xx` alarms (SageMaker 5xx, above 5 per minute for two minutes) and the policy function's `<function>-errors` alarm (Lambda `Errors`, above 5 in five minutes). GCP: the `tenant service server errors` and `gateway server errors` policies (5xx above 5 in five minutes). Local: the Prometheus error counters on the Grafana dashboard | The alarm thresholds in both stacks; the request counts from the guide's capstone step and rollback drill |
| p95 latency, warm | triage | 2 seconds | Exported `LatencyP95Ms` (AWS dashboard) or `<service>-p95` (GCP, alert policy `p95 latency, service-measured`) | The last finite bucket of `nw_triage_latency_seconds`; a p95 beyond it is unmeasurable |
| p95 latency, warm | semantic | 2 seconds | same | The last finite bucket of `nw_semantic_latency_seconds` |
| p95 latency, warm | policy | 8 seconds | same, plus the Lambda duration p95 alarm (AWS) and the Cloud Run `request_latencies` policy (GCP), both at 8 seconds for 15 minutes | The existing latency alarms in both stacks |
| p95 latency, warm | agent, `/route` | 30 seconds | same | `COSTS.md`: about 30 seconds per loop run on the 8 GB function; the guide's "a few seconds for a routed ticket" is the median, not the tail |
| Cold start | every service | Reported, not promised: 20 to 40 seconds for the agent image | The first `/readyz` after a deploy, timed in the guide | `COSTS.md` and the guide's cold-start step |
| Model cost per resolution | agent | At most 1.5 times the baseline in `data/golden/agent_baseline.json`, and never above the deployment's spend cap | Exported `CostUsd` over runs (both dashboards), `cost_usd` on every `/route` response, `make agent-gate`, `NW_SPEND_CAP_USD=25`, the budget at 50, 80 and 100 percent | `max_cost_ratio` in the agent gate; the cap and budget in both stacks; `COSTS.md`: about 3 USD for the 40 capstone runs |
| P0 recall | triage | At least 0.85 on the test split and no more than 0.03 below the committed production summary | The promotion gate (`make promote-triage`, weekly `retrain-triage.yml` against `data/golden/triage_production.json`); in production, the P0 share of predictions against training through the drift PSI | `min_p0_recall` and `max_p0_recall_drop` in `nw/triage/promote.py` |
| P0 recall | semantic | At least 0.70 | `make promote-semantic` | `min_p0_recall` in `nw/semantic/promote.py`; Project 1's threshold rule carries the SLA in front of this model |
| Drift alerts | every service | Zero open `drift_alert` lines; a line is an incident, not a trend | AWS: the `DriftAlerts-<service>` metric filter and `<function>-drift` alarm. GCP: `northwind-drift-alerts` and the `northwind drift` policy. Both dashboards: the exported `DriftLevel` panel (0 ok, 1 watch, 2 alert) | PSI watch 0.1 and alert 0.2 in every monitor; policy refusal rate 2.0 times the baseline; agent cap rate 0.3 and tool error rate 0.2 |
| Approval latency | the escalation queue | Not set. Measure the age of the oldest pending proposal for a month, then set it | `make approve` lists pending proposals from `artifacts/traces`; nothing alarms on their age yet | No number exists; inventing one here would be the wrong kind of SLO |
| Rollout safety | every service | No rollout reaches 100 percent while an errors or p95 alarm is in ALARM | AWS: live endpoints through SageMaker blue/green, 10 percent of capacity for 5 minutes with auto rollback on the 5xx, p95 and drift alarms; the policy function through CodeDeploy, 10 percent for 15 minutes. GCP: Cloud Deploy with approval and a Cloud Run canary at `canary_percent`, the live Agent Platform endpoint by traffic split. Local: the weighted nginx proxy (`make local-canary WEIGHT=10`), rolled back by hand | `serving.py` in the CDK stack; `canary_percent` and the delivery module in Terraform; `deploy/local/proxy/upstream.conf` |

## Error budget policy

The budget is spent when any SLO above is missed: an errors, p95 or drift alarm enters ALARM, a promotion gate fails, or a canary is rolled back. While it is spent:

- Promotions freeze. No approval in the registry, no canary promoted to 100 percent, no `make approve-*`, no `cdk deploy` or `terraform apply` except the fix. On GCP `stable` keeps 100 percent; on AWS the alias stays where the rollback left it.
- Gates stay. A bar is never lowered to ship; a gate that fails is a finding, not an obstacle.
- The operator controls stay available and are used in this order: `NW_AGENT_DISABLED=1` if the agent is the source (runs refused with 503, readiness unchanged), the concurrency cap, then the spend cap. Never the API key.
- Someone reads traces: `make review` samples ten, and the finding goes in the incident note with the deployment id or the gate report.

The budget is restored when the alarm has been OK for a full window (three five-minute periods, the p95 alarm's own evaluation length) and the next gate run passes. Then promotions resume through the same canary, never around it.

## What is not promised

- Anything at the network edge beyond what the platform deploys: the policy API sits behind API Gateway with a Cognito authorizer on AWS, the Cloud Run services have no WAF, and the in-service rate limiter per key is the course's mitigation; `COSTS-platform.md` prices the production options.
- Anything in a higher environment: the course deploys one environment and describes the second (deploy READMEs, Lower and higher environments).
