# deploy/gcp

Terraform for the GCP track. Two tiers in one project:

| Tier | Directory | What it deploys |
| --- | --- | --- |
| `session` | `session/` | Four Cloud Run services (triage, semantic, policy, resolver agent) with least-privilege service accounts, request-based billing, readiness on `/readyz`, Cloud Monitoring dashboard and alert policies, a billing budget |
| `reference` | `reference/` | The resolver on Vertex AI Agent Engine from the same image, a Model Armor template, the MCP tool server on Cloud Run invokable only by the agent, and optionally Cloud SQL with pgvector |

```bash
make images-gcp                # create the Artifact Registry repository if needed, build and push the images
make deploy-gcp TIER=session   # terraform apply after validate and plan
make stop-gcp                  # scale every Cloud Run service to zero (min instances 0 is already zero cost when idle)
make destroy-gcp TIER=session  # terraform destroy, then delete the image repository
```

Read `COSTS.md` first. `make deploy-gcp` runs `terraform validate` and a plan before every apply; review the plan before the first apply.
