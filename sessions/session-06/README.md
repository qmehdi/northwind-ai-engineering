# Session 6: Deployment, observability and capstone

What you build: the whole system on your cloud, and the capstone router in `nw/agent/router.py`.

```bash
make session06                 # router tests, plus the CDK synth review on the aws track
make images && make up-observability
make synth-aws TIER=session    # or: make plan-gcp TIER=session
make deploy-aws TIER=session   # or: make images-gcp && make deploy-gcp TIER=session
make destroy-aws TIER=session  # or: make destroy-gcp TIER=session
```

Read `deploy/COSTS.md` before any deploy. Files you edit: `nw/agent/router.py` (`route`). Files you read: `Dockerfile`, `docker-compose.yml`, `deploy/aws/`, `deploy/gcp/`, `nw/agent/screen.py`.
