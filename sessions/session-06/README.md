# Deployment, observability and capstone

What you build: the capstone router in `nw/agent/router.py`. What you operate: the whole system, promoted to the platform's live target behind a canary, rolled back once on purpose, watched against its objectives, approved from the ops store, and documented for governance.

```bash
make session06               # 57 tests: router, runtime contract, screening, capture redaction, manifest, OpenAPI, decision records;
                             # then the 45-test AWS synth review when deploy/aws/.venv exists
make agentops-check          # catalog, registry, the offline tiered gate (35 cases)
make governance-check        # catalog governance fields, registry, data manifest
```

The platform is already deployed: by the instructor in cohort mode, by you in the pre-work in solo mode, by `make local-up` on the Local track. Read `deploy/COSTS-platform.md` first.

| | AWS | Google Cloud | Azure | Local |
| --- | --- | --- | --- | --- |
| What is running | `uv run python -m nw.platform.aws describe`; `make status-aws` (solo) | `make tenants-gcp`; `make status-gcp` (solo) | `make describe-azure`, `make tenants-azure`; `make status-azure` (solo) | `make local-status` |
| Nothing to promote | `make bootstrap-aws NAMES=triage` | `make bootstrap-gcp NAMES=triage` | `make bootstrap-azure NAMES=triage` | `make pipeline-submit PIPELINE=triage ARGS="--wait"` |
| Promote a model | `promotion_candidate`, then `set_stage(..., LIVE)`: blue/green canary on `northwind-live-triage` | `endpoints.deploy(..., live=True, canary_percent=10)`, then `promote` or `rollback` | the same on `nw-live-triage-<scope>` (instructor in cohort mode) | `endpoints.deploy(..., canary_percent=10)`, then `live=True` |
| Release images | `make release-aws`, `make approve-aws` | `make release-gcp`, `make approve-gcp` twice | `make release-azure`, `make approve-azure` | `make local-up` |
| Rolls back | automatically on 5xx, p95 and the quality level (not drift) | a person, told by the alerts | a person; the alerts block the approval | a person, `make local-canary WEIGHT=0` |
| Approve a proposal | `make approve` with `NW_OPS_STORE`, `NW_ENVIRONMENT`, `NW_TENANT` | the same | the same | `make approve` |
| Rotate a key | `make rotate-key TRACK=aws TENANT=<handle> DRY_RUN=1` | `TRACK=gcp` | `TRACK=azure` | no keys on Local |
| Higher environment | described, `deploy/aws/README.md` | described, `deploy/gcp/README.md` | described, `deploy/azure/README.md` | `make local-higher-up`, `make local-promote` |
| Stop (solo) | `make stop-aws`, `make destroy-aws` | `make stop-gcp`, `make destroy-gcp` | `make destroy-azure` stops every meter | `make local-down` |

Files you edit: `nw/agent/router.py` (`_route`), and `NOTES.md` in this directory, the runbook each step writes into (add an "Operations" and a "Governance" section to the template). Files you read: `deploy/SLO.md`, the deploy README of your track, `docs/adr/0005-approval-gate-enforced-twice.md`, `docs/governance/`, `docs/SECURITY.md`, `nw/platform/base.py` (the stage rules and `promotion_candidate`), `nw/agent/approve.py`, `nw/agent/opstore.py`.
