# deploy/aws

CDK (Python) for the AWS track. Two tiers from the same images:

| Tier | Stack | What it deploys |
| --- | --- | --- |
| `session` | `northwind-session` | Four Lambda container-image functions (triage, semantic, policy, resolver agent) on arm64 with function URLs, an API key in Secrets Manager, least-privilege execution roles, OpenTelemetry spans to X-Ray with Transaction Search, CloudWatch dashboard and alarms, SNS, a monthly budget |
| `reference` | `northwind-reference` | The session services plus AgentCore: a tools runtime (MCP) and an agent runtime, a Gateway with a Cedar policy engine in ENFORCE mode, a Bedrock Guardrail, an S3 Vectors index, an Agent Registry record |

```bash
make deploy TIER=session      # cdk deploy after synth and cdk-nag
make stop                      # reserved concurrency 0 on every function; idle already costs nothing
make start
make destroy TIER=session
```

Before the first deploy of either tier, read `COSTS.md` and run `make synth` and `pytest deploy/aws/tests`. The synth test refuses wildcard model access, any image that is not arm64, and any template that carries the API key value instead of the secret's ARN.

App Runner, which this path used first, is closed to new AWS customers. ECS Express Mode is the always-warm alternative if a cohort needs no cold starts; it is not in this repo because it bills while idle.
