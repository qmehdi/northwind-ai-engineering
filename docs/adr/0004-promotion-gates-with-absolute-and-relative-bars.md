# 0004. Promotion gates with absolute and relative bars

Date: 2026-09-10. Status: accepted.

## Context

A retrained model is promoted to serving by a gate, in the retraining workflows and on a laptop. A gate that only compares with the current production model lets a slow slide through: each candidate a little worse than the last, each within tolerance. A gate that only checks fixed thresholds lets a bad regression through as long as it clears them: macro-F1 down four points is still above the bar. Northwind's real risk sits in one class, P0, where a miss is an outage answered as a feature request, and in calibration, because the router in the capstone trusts the confidence.

## Decision

Every gate (`nw/triage/promote.py`, `nw/semantic/promote.py`, `nw/policy/evaluate.py`, `nw/agent/evaluate.py --gate`) applies two kinds of bar and reports every reason it fails. Absolute bars protect Northwind and never move with the candidate: P0 recall at least 0.85, expected calibration error at most 0.12, for the agent no escalation on an injected ticket. Relative bars protect against regression and compare with the committed production summary on the same test split: macro-F1 within 0.02, Brier within 0.01, P0 recall within 0.03, for the semantic model the int8 graph within 0.03 of fp32, for retrieval recall@k and MRR within 0.03 of the baseline. A candidate passes only when it clears both kinds. `--force` promotes anyway and records it as forced in the summary and the model card; it exists so a person can decide, not so a script can.

## Consequences

- A gate failure is a list of reasons, and the model card carries it, so a rejected candidate is a document, not a log line.
- The production summaries are committed data (`data/golden/*_production.json`, `data/golden/baseline.json`, `data/golden/agent_baseline.json`) and covered by the data manifest, so moving a bar or a baseline is a reviewed change.
- Relative bars are valid only on the same split; a changed dataset version invalidates the comparison and the gate says so.
- Pass counts on the agent's fifteen cases move by one between identical runs; the agent gate allows one, and a failure on the count alone is a reason to rerun before it is a reason to stop a release.
