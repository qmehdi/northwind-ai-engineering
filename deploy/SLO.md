# Service level objectives

What the Session path promises, per service, and what measures each promise. Every number below already exists somewhere in this repo: an alarm threshold in `deploy/aws/stacks/session_path.py` or `deploy/gcp/session/main.tf`, a gate bar in `nw/triage/promote.py`, `nw/semantic/promote.py` or `nw/agent/evaluate.py`, a drift bar in `nw/*/monitor.py`, a measurement the guide asks for, or a line in `COSTS.md`. Nothing has been measured in production yet; the validation run and the first month of traffic replace the targets that say so.

## SLIs and SLOs

| SLI | Service | SLO | Measured by | Where the number comes from |
| --- | --- | --- | --- | --- |
| Availability: share of requests answered without a 5xx | every service | No five-minute window with more than five 5xx responses. At the capstone's rate (about 40 requests in five minutes) that is 87.5 percent of requests; at one probe every two seconds it is 96.7 percent. Set a percentage after the first month of real traffic | AWS: the `<function>-errors` alarm (Lambda `Errors`, Sum over 5 minutes, above 5, two periods) and the exported `Errors` over `Requests`. GCP: the `server errors` policy on `run.googleapis.com/request_count` 5xx and the log-based `<service>-errors` over `<service>-requests` | The alarm thresholds in both stacks; the request counts from the guide's capstone step and rollback drill |
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
| Rollout safety | every service | No rollout reaches 100 percent while an errors or p95 alarm is in ALARM | AWS: CodeDeploy, 10 percent for 15 minutes, rollback on the two alarms. GCP: `CANARY=10`, the request and latency panels split by revision, `stable` keeps 90 | `DEPLOYMENT_CONFIG` in the CDK stack; `canary_percent` in the module |

## Error budget policy

The budget is spent when any SLO above is missed: an errors, p95 or drift alarm enters ALARM, a promotion gate fails, or a canary is rolled back. While it is spent:

- Promotions freeze. No `make promote-*`, no canary promoted to 100 percent, no `cdk deploy` or `terraform apply` that changes a function except the fix. On GCP `stable` keeps 100 percent; on AWS the alias stays where the rollback left it.
- Gates stay. A bar is never lowered to ship; a gate that fails is a finding, not an obstacle.
- The operator controls stay available and are used in this order: `NW_AGENT_DISABLED=1` if the agent is the source (runs refused with 503, readiness unchanged), the concurrency cap, then the spend cap. Never the API key.
- Someone reads traces: `make review` samples ten, and the finding goes in the incident note with the deployment id or the gate report.

The budget is restored when the alarm has been OK for a full window (three five-minute periods, the p95 alarm's own evaluation length) and the next gate run passes. Then promotions resume through the same canary, never around it.

## What is not promised

- Anything at the network edge: function URLs and public Cloud Run have no throttling or WAF. The in-service rate limiter per key is the course's mitigation; `COSTS.md` prices the production options.
- Anything about the Reference stack, which is deployed for demos and destroyed the same day.
