# deploy/gcp

Terraform for the GCP track. Two tiers in one project:

| Tier | Directory | What it deploys |
| --- | --- | --- |
| `session` | `session/` | Four Cloud Run services (triage, semantic, policy, resolver agent) with least-privilege service accounts, request-based billing, readiness on `/readyz`, revision tags `latest` and `stable` with an optional traffic split, log-based metrics from each service's exported `metrics_snapshot` line, Cloud Monitoring dashboard and alert policies, a billing budget |
| `reference` | `reference/` | The resolver on Vertex AI Agent Engine from the same image, a Model Armor template, the MCP tool server on Cloud Run invokable only by the agent, and optionally Cloud SQL with pgvector |

```bash
make images-gcp                # create the Artifact Registry repository if needed, build and push the images
make deploy-gcp TIER=session   # terraform apply after validate and plan
make stop-gcp                  # scale every Cloud Run service to zero (min instances 0 is already zero cost when idle)
make destroy-gcp TIER=session  # terraform destroy, then delete the image repository
make deploy-gcp CANARY=10      # newest revision on 10 percent under the tag latest, the previous one on 90 under stable
TRACK=gcp scripts/rotate_key.sh   # new secret version, a new revision per service, older versions disabled
NW_STAGE=staging make deploy-gcp  # northwind-staging-* names in the same project
```

Images are tagged with the git SHA (`-dirty` when the tree has changes) by `make images-gcp`, and `make deploy-gcp` defaults to the same tag, so a revision names the code it runs; `NW_IMAGE_TAG=latest` overrides both. A canary needs a service that already exists (the first apply runs with `CANARY=0`); promote with `CANARY=0`, or roll back with `gcloud run services update-traffic <service> --to-tags stable=100`, which the next apply overrides. A stage (lowercase, at most 7 characters) goes after `northwind` in every name; stages share the image repository, which only the unstaged destroy deletes.

Read `COSTS.md` first. `make deploy-gcp` runs `terraform validate` and a plan before every apply; review the plan before the first apply.
