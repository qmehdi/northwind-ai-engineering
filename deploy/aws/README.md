# deploy/aws

CDK (Python) for the AWS track. Two tiers from the same images:

| Tier | Stack | What it deploys |
| --- | --- | --- |
| `session` | `northwind-session` | Four App Runner services (triage, semantic, policy, resolver agent), least-privilege instance roles, X-Ray, CloudWatch dashboard and alarms, SNS, a monthly budget |
| `reference` | `northwind-reference` | The session services plus AgentCore: a tools runtime (MCP) and an agent runtime, a Gateway with a Cedar policy engine in ENFORCE mode, a Bedrock Guardrail, an S3 Vectors index, an Agent Registry record |

```bash
make deploy TIER=session      # cdk deploy after synth and cdk-nag
make stop                      # pause every App Runner service (still pays provisioned memory)
make start
make destroy TIER=session
```

Before the first deploy of either tier, read `COSTS.md` and run `make synth` and `pytest deploy/aws/tests`. The synth test refuses wildcard model access and any runtime that is not arm64.
