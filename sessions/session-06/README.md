# Deployment, observability and capstone

What you build: the capstone router in `nw/agent/router.py`, and the promotion of approved artifacts to the platform's live target.

```bash
make session06               # router, runtime contract, screening, vectors; plus the AWS synth review when deploy/aws/.venv exists
make agent-cards && make registry-check
```

The platform is already deployed: by the instructor in cohort mode, by you in Part 0 in solo mode, by `make local-up` on the Local track. Read `deploy/COSTS-platform.md` first.

| | AWS | Google Cloud | Local |
| --- | --- | --- | --- |
| What is running | `uv run python -m nw.platform.aws describe`; `make status-aws` (solo) | `make tenants-gcp`; `make status-gcp` (solo) | `make local-status` |
| Promote a model | `set_stage(..., Stage.LIVE)`: blue/green canary on `northwind-live-triage` | `endpoints.deploy(..., live=True, canary_percent=10)`, then 0 | `endpoints.deploy(..., canary_percent=10)`, `make local-canary WEIGHT=10` |
| Release images | push to `main`, approve `northwind-delivery` | `make release-gcp`, `make approve-gcp` | `make local-up` |
| Higher environment | described, `deploy/aws/README.md` | described, `deploy/gcp/README.md` | `make local-higher-up`, `make local-promote` |
| Stop (solo) | `make stop-aws`, `make destroy-aws` | `make stop-gcp`, `make destroy-gcp` | `make local-down` |

Files you edit: `nw/agent/router.py` (`route`), and `NOTES.md` in this directory for the numbers the session check reads. Files you read: `deploy/SLO.md`, the deploy README of your track, `nw/agent/agentcore.py`, `nw/agent/screen.py`.
