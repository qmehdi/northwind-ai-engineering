# Service level objectives

What the platform promises, per service, how each promise is measured, and what happens when one is missed. It covers the four tracks (AWS, Google Cloud, Azure, Local) of the managed platform in ADR 0008 to 0013. Nothing has been measured in production yet: the validation run per track and the first month of traffic replace every target marked "set after a month". The model and platform prices behind the cost numbers are in `COSTS-platform.md`.

Three kinds of objective, because a model service can fail in three ways:

- **Service**: it answers, and fast enough. Ratio SLIs over request counts.
- **Quality at release**: the version that ships is as good as the one it replaces, on held-out data, with the evidence stated. The promotion and regression gates.
- **Quality in production**: the version that is serving is still as good as it was measured. The quality canary signals and the delayed-label loop.

## Service SLIs and SLOs

Every SLI is a ratio of good events to valid events over a rolling 28-day window, computed from the exported `metrics_snapshot` lines (`nw/metrics_export.py`: `Requests`, `Errors`, `LatencyCount` and the latency histogram per service and stage).

| SLI | Service | SLO (28 days) | Good events / valid events |
| --- | --- | --- | --- |
| Availability | triage, semantic | 99.5 percent | requests without a 5xx / all requests |
| Availability | policy | 99.0 percent | requests without a 5xx / all requests (a refusal is a good event) |
| Availability | agent `/route` | 99.0 percent | runs that ended in `answer` or a proposal / all runs (a cap or an error is bad) |
| Latency | triage, semantic | 95 percent under 2 s, warm | requests under 2 s / all requests |
| Latency | policy | 95 percent under 8 s, warm | requests under 8 s / all requests |
| Latency | agent `/route` | 95 percent under 30 s, warm | runs under 30 s / all runs |
| Cold start | every service | Reported, not promised: 20 to 40 s for the agent image | the first `/readyz` after a deploy |
| Cost per resolution | agent | At most 1.5 times the track baseline, never above the spend cap | `CostUsd` over runs; `max_cost_ratio` in the agent gate |

The targets are deliberately modest: the course runs one environment per track on scale-to-zero serving, and a 99.9 percent promise on a cold-starting function would be spent by its first cold start.

### Burn-rate alerting

An error budget of 0.5 percent over 28 days is 3.4 hours of full outage. Alert on how fast it burns, not on raw counts, with two windows per alert so a short spike does not page and a slow leak is not missed:

| Alert | Burn rate | Long window | Short window | Budget spent when it fires | Action |
| --- | --- | --- | --- | --- | --- |
| Page | 14.4 | 1 hour | 5 minutes | 2 percent | a person, now |
| Ticket | 6 | 6 hours | 30 minutes | 5 percent | a person, today |

Burn rate is the error ratio over the window divided by the budget ratio (1 minus the SLO). A window with fewer than 100 valid events does not fire either alert: at the capstone's traffic (about 40 requests in five minutes) one 5xx is a 2.5 percent error ratio, which is a burn rate of 5 on a single event. Where each track computes it: AWS, CloudWatch metric math over the EMF metrics in namespace `Northwind` (dimensions `Service`, `Stage`); Google Cloud, a request-based SLO on the log-based metrics with burn-rate alert policies; Azure, log alert rules over the Log Analytics workspace; Local, Prometheus recording rules and Grafana alerts. The IaC in `deploy/aws`, `deploy/gcp/platform`, `deploy/azure` and `deploy/local` owns the resources; this table owns the numbers.

## Quality at release

Every gate states its sample and its evidence: rates with counts and 95 percent Wilson intervals, tolerances in cases or tickets rather than a rate smaller than one case, and paired tests (McNemar, paired bootstrap) when champion and challenger are scored on the same items. A comparison the gate cannot trust (no provenance, a legacy baseline, other models, another corpus or case set) fails with a reason.

| Objective | Service | Bar | Measured by |
| --- | --- | --- | --- |
| P0 recall on the test split | triage | at least 0.85, and at most one more missed P0 ticket than production, paired on the same rows | `nw/triage/promote.py`; `make promote-triage`; weekly `retrain-triage.yml` when the trigger fires |
| P0 recall per language, gate set (test split plus `data/golden/triage_slices.jsonl`) | triage | at least 0.70 once a language has 10 P0 tickets; fewer is reported as insufficient evidence; a written waiver is shown on the card and every decision (German today) | same |
| P0 recall of the served graph | semantic | at least 0.70 on the graph the service runs (int8 when it clears every bar, else fp32, in `serving.json`); int8 at most one more missed P0 ticket than fp32 | `nw/semantic/promote.py` |
| Retrieval on the held-out gate split | policy | no more than one case lost on full recall and on recall at 1; MRR within 0.03 | `eval-gate.yml` retrieval job on every pull request, against `data/golden/baselines/policy-retrieval_only.json` |
| Generation on the gate split | policy | refusal correctness: no more than one case lost; key-fact correctness, citation recall within 0.03; citation validity 1.0 | `eval-gate.yml` generation job on every prompt change, against `policy-<track>-no_judge.json` |
| Faithfulness | policy | within 0.03 of the baseline, gated only when the Judge's calibration report measured the same Judge id | `make eval-policy` judged run; `nw/policy/calibrate.py` |
| pass^k on the adversarial and benign cases | agent | pass^3 at most one case below the track baseline; every injection case passed on every run; zero unapproved executions | `agent-gate.yml` live job, `--repeats 3` |
| Turn judge mean | agent | at least 3.5, gated only when `--calibrate-judge` measured the same Judge id | same |

## Quality in production: the canary signals

The monitors compute these in the service (`nw/*/monitor.py`), export them through `metrics_snapshot` and log `quality_alert` (fields `signal`, `value`, `bar`, `window`, `service`, `tenant`, `environment`) when one crosses its bar, at most once a minute per signal. A canary bakes on them as well as on 5xx and latency.

| Signal | Service | Metric (Prometheus / exported field / CloudWatch name) | Bar |
| --- | --- | --- | --- |
| Quality level | every service | `nw_<service>_quality_level` / `quality_level` / `QualityLevel` | 2 is an alert (0 ok, 1 watch, -1 warming up) |
| Shadow agreement | triage, semantic | `nw_<service>_shadow_agreement` / `shadow_agreement` / `ShadowAgreement` | below 0.90 over at least 200 shadowed requests |
| Predicted P0 share | triage, semantic | `nw_<service>_predicted_p0_share` / `p0_share` / `P0Share` | reported; the bar is on the ratio |
| P0 share ratio | triage, semantic | `nw_<service>_p0_share_ratio` / `p0_share_ratio` / `P0ShareRatio` | 2 or above, or 0.5 or below, with the validation share outside the window's 99 percent interval |
| Refusal rate | policy | `nw_policy_refusal_rate` / `refusal_rate` / `RefusalRate` | reported; the bar is on the ratio |
| Refusal ratio | policy | `nw_policy_refusal_ratio` / `refusal_ratio` / `RefusalRatio` | 2 or above, with the baseline rate outside the window's 99 percent interval |
| Sampled judge score | agent | `nw_agent_judge_score` / `judge_score` / `JudgeScore` | below 3.5 over at least 20 judged runs |

The platforms' own monitors measure something else and are read on their own scale: SageMaker Model Monitor's `feature_baseline_drift_<feature>` is a distance between the baseline and the current distribution (for a numeric feature, the largest gap between the two cumulative distributions), Vertex AI Model Monitoring reports Jensen-Shannon or L-infinity distances, and Azure Machine Learning data drift a per-feature distance. None of them is PSI, so their thresholds are set against their own measurements after a month of traffic, not copied from the PSI bar of 0.2.

Every window waits for 200 requests (the agent's rates for 50 runs), and every PSI bar is the fixed 0.2 or the level a window of that size reaches by chance, whichever is higher: at 50 requests about one stationary snapshot in four crossed 0.2, which made the old drift alarm a noise generator.

**Bake time is sized to traffic.** A signal needs its window. At a 10 percent canary and the capstone's rate (about 8 requests a minute), 200 canary requests take four hours, and a 5xx alarm at more than five a minute can never fire. So a course canary either sends the golden cases through the canary while it bakes (the policy gate split, the agent's cases, a slice of the triage test split: synthetic load the platform's release step runs) or bakes at 50 percent, and says which. A production canary bakes until the canary has served 200 requests or its 5xx ratio could have shown a burn rate of 14.4, whichever takes longer.

## Delayed labels and retraining

A support ticket's final priority is known days later. `python -m nw.triage.monitor quality` joins captured predictions (`NW_TRIAGE_CAPTURE`, keyed by `ticket_id` or `correlation_id`) with the priorities people set, once a label is at least three days old, and reports live P0 recall and precision with intervals and label coverage. `python -m nw.triage.monitor trigger` decides whether to retrain: new data, a central drift or quality alert, live P0 recall below 0.85 on at least 20 labelled P0 tickets, or 500 new labels. `retrain-triage.yml` runs the trigger first and skips a scheduled run when nothing changed, because the same data trains the same model.

Drift and quality windows live in one process and reset on a cold start. The central path is the capture: every instance writes its predictions to the capture file in object storage, and `python -m nw.triage.monitor drift --capture <files>` computes the same snapshot over all of them. The policy service's drift baseline starts as the golden set's (over a quarter must-refuse questions, so not traffic) and is replaced after a week by `build_index --capture <NW_POLICY_CAPTURE file>`; `/drift` says which baseline it uses.

**MLOps maturity, stated plainly.** The course builds the Level 1 pieces of the usual maturity ladder for Projects 1 and 2: a pipeline that validates, trains, evaluates and gates without hands, a registry, a human approval, and monitoring. Continuous training is triggered by new data today; drift and label-volume triggers exist in code and fire once the operator schedules the central drift and quality jobs against the platform's capture bucket. Promotion always needs a person. The course does not claim Level 2 (the pipeline's own delivery automated from monitoring), and it does not claim live quality numbers until a month of labelled traffic exists.

## Error budget policy

The budget is spent when the page alert fires, a promotion gate fails, a canary is rolled back, or a quality signal is in alert for a full window. While it is spent:

- Promotions freeze. No approval in the registry, no canary promoted to 100 percent, no `make approve-*`, no `cdk deploy`, `terraform apply` or `az deployment` except the fix. The live version stays where the rollback left it.
- Gates stay. A bar is never lowered to ship; a gate that fails is a finding, not an obstacle. A waiver is a written, dated decision shown on the model card, not a changed number.
- The operator controls stay available and are used in this order: `NW_AGENT_DISABLED=1` if the agent is the source (runs refused with 503, readiness unchanged), the concurrency cap, then the spend cap. Never the API key.
- Someone reads traces: `make review` samples ten, and the finding goes in the incident note with the deployment id or the gate report.

The budget is restored when the burn-rate alerts have been quiet for a full long window and the next gate run passes. Then promotions resume through the same canary, never around it.

## What is not promised

- Anything at the network edge beyond what the platform deploys: the public endpoints are behind keys and the in-service rate limiter per key is the course's mitigation; `COSTS-platform.md` prices the production options.
- Approval latency: measure the age of the oldest pending proposal for a month, then set it. Inventing a number now would be the wrong kind of SLO.
- Anything in a higher environment: the course deploys one environment per track and describes the next one (deploy READMEs, Lower and higher environments).
