# deploy/aws

CDK (Python) for the AWS track. Two tiers from the same images:

| Tier | Stack | What it deploys |
| --- | --- | --- |
| `session` | `northwind-session` | Four Lambda container-image functions (triage, semantic, policy, resolver agent) on arm64, each behind a published version and the alias `live` with a function URL on the alias, a CodeDeploy deployment group per function (10 percent for 15 minutes, rollback on the errors or p95 alarm), an API key in Secrets Manager, least-privilege execution roles, OpenTelemetry spans to X-Ray with Transaction Search, EMF metric lines into the `Northwind` namespace, CloudWatch dashboard and alarms, SNS, a monthly budget |
| `reference` | `northwind-reference` | The session services plus AgentCore: a tools runtime (MCP) and an agent runtime, a Gateway with a Cedar policy engine in ENFORCE mode, a Bedrock Guardrail, an S3 Vectors index filled by the deploy script, one log group for the runtimes |

```bash
make setup-aws                 # deploy/aws/.venv and the CDK CLI pinned in package.json (Node 22 or later)
make deploy-aws TIER=session   # synth test, cdk bootstrap, Transaction Search, cdk deploy
make stop-aws                  # reserved concurrency 0 on every function; idle already costs nothing
make start-aws
make destroy-aws TIER=session
scripts/deploy_aws.sh rollout agent        # publish $LATEST as a version and shift the live alias through CodeDeploy
scripts/deploy_aws.sh deployments agent    # the last five deployments and their rollback messages
TRACK=aws scripts/rotate_key.sh            # new secret value, every alias moved to a version that reads it
NW_STAGE=staging make deploy-aws           # northwind-staging-* names in the same account
```

A later `cdk deploy` that changes a function's image or configuration moves the alias through CodeDeploy as well, so the deploy waits about 15 minutes for the shift. With the default empty stage every name is the one the guide uses; a stage (lowercase, at most 7 characters) goes after `northwind` in the stack name, function names, log groups, alarms, dashboard, budget and CodeDeploy application, and the outputs file key becomes `northwind-<stage>-session`.

`make deploy-aws` writes `outputs.json` here with `UrlTriage`, `UrlSemantic`, `UrlPolicy`, `UrlAgent` and `ApiKeySecretArn` (the function URLs carry a trailing slash). The first deploy of a tier creates IAM roles, so `cdk deploy` stops for a y/n once. After a reference deploy the script publishes the policy chunks to the S3 Vectors index with `nw.policy.publish_vectors`. The resolver runtime serves the AgentCore HTTP contract from `nw.agent.agentcore:app` (`GET /ping`, `POST /invocations` on 8080).

Before the first deploy of either tier, read `COSTS.md` and run `make synth-aws`. The synth test refuses wildcard model access, any image that is not arm64, and any template that carries the API key value instead of the secret's ARN.

App Runner, which this path used first, is closed to new AWS customers. ECS Express Mode is the always-warm alternative if a cohort needs no cold starts; it is not in this repo because it bills while idle.
