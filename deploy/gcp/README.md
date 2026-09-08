# deploy/gcp

Terraform for the GCP track. Two tiers in one project:

| Tier | Directory | What it deploys |
| --- | --- | --- |
| `session` | `session/` | Artifact Registry, four Cloud Run services (triage, semantic, policy, resolver agent) with least-privilege service accounts, request-based billing, readiness on `/readyz`, Cloud Monitoring dashboard and alert policies, a billing budget |
| `reference` | `reference/` | The resolver on Vertex AI Agent Engine from the same image, a Model Armor template, the MCP tool server on Cloud Run invokable only by the agent, and optionally Cloud SQL with pgvector |

```bash
make images-gcp                # build and push the images to Artifact Registry
make deploy-gcp TIER=session   # terraform apply after validate and plan
make stop-gcp                  # scale every Cloud Run service to zero (min instances 0 is already zero cost when idle)
make destroy-gcp TIER=session
```

Read `COSTS.md` first. `terraform validate` runs in CI; `terraform plan` output is reviewed before the first apply.
