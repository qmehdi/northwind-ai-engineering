# deploy/aws

CDK (Python) for the AWS track: one platform stack per environment (ADR 0005, 0008, 0009, 0012),
`northwind-platform` by default, `northwind-<env>-platform` with `NW_ENV=<env>`. The stack is
`stacks/platform.py`; each area of the reference architectures is a construct in `stacks/areas/`.

| Area | Construct | What it deploys |
| --- | --- | --- |
| Data and governance | `data.py` | KMS key; `data`, `artifacts` and access-logs buckets (KMS, versioned, SSL only); Glue database and the `tickets` table; Lake Formation registration off by default |
| Tracking and registry | `tracking.py` | SageMaker domain (IAM), a user profile and an execution role per tenant scoped to `northwind-<tenant>-*`, the managed MLflow tracking server, a Model Package Group per project and tenant |
| Pipelines | `pipelines.py` | A weekly EventBridge schedule per tenant that starts `northwind-<tenant>-triage`, disabled until enabled on purpose |
| Serving | `serving.py`, `functions/deploy_on_approval` | The approval-driven deployer (serverless endpoint for a tenant, canary and autoscaling and Model Monitor for `live`), the live endpoints' alarms, the policy service as a Lambda behind an HTTP API with Cognito, CodeDeploy canary on its alias |
| Prompts and retrieval | `prompts.py` | Bedrock prompts from `prompts/catalog.json` with a version each; an S3 Vectors bucket, one index and one Knowledge Base per tenant and for `live` |
| Agents | `agents.py` | AgentCore Runtime (tools as MCP, the live resolver), Gateway with a Cedar policy engine, Memory per tenant, Identity (workload identity, API key credential provider), Evaluations (custom judge, online config), the AWS Agent Registry with the tools and resolver records, the guardrail |
| Model gateway | `gateway.py` | LiteLLM on ECS Fargate behind an ALB, Aurora Serverless v2 for virtual keys, the master key in Secrets Manager, an application inference profile per tenant and role |
| Delivery | `delivery.py`, `delivery/deploy.sh` | ECR repositories, CodePipeline V2 from GitHub (CodeConnections), CodeBuild on arm64, manual approval, deploy by digest with the CodeDeploy canary, the `northwind-deployer` role |
| Identity and observability | `identity.py` | Cognito user pool for the web entry, CloudTrail, the platform dashboard, the alerts topic, the budget |

```bash
make setup-aws                        # deploy/aws/.venv and the CDK CLI pinned in package.json (Node 22 or later)
make platform-aws-test                # the synth review (cdk-nag on) and the client tests, no account
make synth-aws                        # NW_MODE=solo (default) or NW_TENANTS=alice,bob
NW_TENANTS=alice,bob NW_ALERT_EMAIL=you@example.com make deploy-aws
make tenants-aws ACTION=add TENANT=carol
make status-aws
make stop-aws                         # idle cost to the floor; make start-aws brings it back
make destroy-aws
```

Context: `-c tenants=a,b` (cohort) or `-c mode=solo`; `-c env=<word>`; `-c alertEmail`;
`-c budgetUsd`; `-c connectionArn` (the delivery pipeline, see below); `-c lakeFormation=true`;
`-c githubOwner`, `-c githubRepo`, `-c githubBranch`. `scripts/deploy_aws.sh` maps them from
`NW_TENANTS`, `NW_MODE`, `NW_ENV`, `NW_ALERT_EMAIL`, `NW_BUDGET_USD`, `NW_CONNECTION_ARN` and
`NW_LAKE_FORMATION`.

## Prerequisites

- An account with Bedrock model access granted for the three course models and Titan Text
  Embeddings v2 in the region, and the AgentCore, Agent Registry and S3 Vectors services
  available there (us-east-1 has all of them).
- Docker running (the policy and agent images are built at deploy), Node 22 or later, `uv`.
- `make setup-aws`, then `aws configure` for the deploying identity; the first deploy runs
  `cdk bootstrap` and turns on CloudWatch Transaction Search for the account.
- The delivery pipeline needs a CodeConnections connection to the GitHub repository, and the
  handshake is manual: create it in the console (Developer Tools, Settings, Connections, GitHub),
  choose "Update pending connection", install the GitHub app on the repository, then pass the
  connection ARN as `NW_CONNECTION_ARN`. Without it the stack deploys everything but the
  pipeline (repositories and the deployer role stay), which is what a solo learner does.
- Model Monitor requires an account that already used it (AWS closed it to new customers in
  2026); on a new account the deployer's monitoring schedule fails to create and the endpoint
  still serves, so treat the drift alarm as a reference-only panel there.

`make deploy-aws` runs the synth review, deploys, mints a model gateway virtual key per tenant
and for `live` (`scripts/gateway_keys.sh`, stored as `northwind-<owner>-gateway-key`), uploads the
tickets and the policy corpus under every owner's prefix and starts one ingestion job per
knowledge base. It writes `outputs.json` here; `nw/platform/aws.py` reads it, and `outputs.json`
plus `.env` are the two files a learner needs.

## The tenant workflow

In cohort mode the instructor owns the platform and each learner is a tenant named after their
handle. What a tenant gets, all named `northwind-<tenant>-...`:

| Resource | Name | Made by |
| --- | --- | --- |
| Studio profile and execution role | `northwind-<tenant>`, `northwind-<tenant>-sagemaker` | the stack |
| Model Package Groups | `northwind-<tenant>-triage`, `-semantic` | the stack |
| Pipelines | `northwind-<tenant>-triage`, `-semantic` | the tenant, `nw/pipelines` through the execution role |
| Retraining schedules | `northwind-<tenant>-retrain-triage`, `-retrain-semantic` (disabled, `Trigger=schedule`) | the stack; the gates read `s3://<artifacts>/baselines/*_production.json`, also from the stack |
| Serverless endpoints | `northwind-<tenant>-triage`, `-semantic` | the approval Lambda when a package is approved |
| Knowledge base and S3 Vectors index | `northwind-<tenant>-policies`, `<tenant>-policies` | the stack; filled by `VectorStore.upsert` |
| Prompts | `northwind-<tenant>-<name>` | the tenant through `PromptStore` |
| Inference profiles and gateway key | `northwind-<tenant>-{workhorse,judge,economy}`, secret `northwind-<tenant>-gateway-key` | the stack; the key by `gateway_keys.sh` |
| AgentCore Memory | `northwind_<tenant>_memory` | the stack |
| Resolver runtime and registry record | `northwind_<tenant>_resolver`, record `northwind-<tenant>-resolver` | the tenant through `AgentRuntime.deploy` and `register` |

The learner's `.env`: `NW_TRACK=aws`, `NW_TENANT=<handle>`, `NW_ENVIRONMENT=northwind`,
`NW_GATEWAY_URL=<GatewayUrl output>`, `NW_GATEWAY_KEY=<their virtual key>` (the instructor sends
it from Secrets Manager), and the AWS credentials the instructor issues for the tenant role.
`make tenants-aws ACTION=add TENANT=<handle>` adds a learner after the first deploy (a redeploy,
about ten minutes, plus their key and corpus); `ACTION=remove` deletes their endpoints, runtime and
key first, then redeploys without them. Solo mode is the same stack with one tenant named `solo`
and `NW_AWS_DIRECT_DEPLOY` on by default, so `EndpointClient.deploy` creates the serverless
endpoint itself instead of waiting for the approval event.

## The promotion drill inside one environment

Promotion is an approval, a canary and a human, exercised without a second account:

1. A pipeline run registers a candidate: `northwind-<tenant>-triage` version N,
   `PendingManualApproval`, metrics in the package's metadata and the artifact under
   `tenants/<tenant>/triage/`.
2. The tenant approves it (`ModelRegistry.set_stage(..., Stage.APPROVED, reason)` or the
   console). The `SageMaker Model Package State Change` event reaches `northwind-deploy-on-approval`,
   which creates or updates the tenant's serverless endpoint.
3. Promotion to live (`Stage.LIVE`) re-registers the same artifact, already approved, in
   `northwind-live-triage`. The same event reaches the same function, which this time updates
   `northwind-live-triage` with a blue/green canary: 10 percent of capacity for 5 minutes, then
   the rest, rolled back on `northwind-live-triage-5xx`, `-p95` or `-drift`. CodeDeploy has no
   SageMaker platform; the endpoint canary is SageMaker's own deployment guardrail
   (`DeploymentConfig.BlueGreenUpdatePolicy`), driven by that function. The first live deploy has
   no blue fleet: it creates the endpoint with data capture, a scaling target and the Model
   Monitor schedule.
4. Code changes take the other road: a push to `main` starts `northwind-delivery`, CodeBuild
   builds both images on arm64 and pushes them by digest, the `Approve` stage waits for a person
   (the alerts topic is notified), and `delivery/deploy.sh` moves the policy Lambda's `live` alias
   through CodeDeploy (10 percent for 15 minutes, rollback on its errors or p95 alarm) and updates
   the live resolver runtime to the new digest. The deploy stage assumes `northwind-deployer`
   first: the same STS hop a lower environment makes into a higher one.
5. Prompts and agents promote the same way: `PromptStore.set_stage(..., Stage.LIVE)` tags the
   version, `AgentRuntime.register` submits the agent card to `northwind-agents`, where a curator
   approves it; an agent that is not approved in the registry is not discoverable.

## Lower and higher environments

The course builds one environment. An organisation with dev, pre-prod and prod deploys this
stack once per environment (`NW_ENV=dev`, `NW_ENV=prod`, one account each, or one account with
distinct names) and changes three things, all present here already:

- The delivery pipeline lives in the lowest environment (or a tooling account). Its Deploy stage
  gains one action per higher environment, and each assumes that environment's
  `northwind-<env>-deployer` role, whose trust policy names the pipeline's role in the lower
  account instead of `AccountPrincipal(self)`. The artifact bucket's KMS key policy grants the
  higher accounts `kms:Decrypt`, which is why the bucket is KMS encrypted with the platform key
  rather than the default.
- Images are promoted by digest, never rebuilt: the higher environment's ECR repositories get a
  replication rule from the lower one, and `delivery/deploy.sh` runs unchanged with the higher
  environment's function and runtime names.
- The shared services (registry, gateway, observability, identity) move to a shared-services
  account; the tenants' resources stay in dev; prod holds only `live`. In the figures the
  dashed box around the registry, the model gateway and Cognito is that account.

Nothing else changes: the same event drives the same Lambda, the same alarms roll back the same
canary, the Agent Registry can be shared across accounts with AWS RAM, and Cost Explorer groups
by the `nw:tenant` tag on the inference profiles.

## Costs, stop and destroy

Read `deploy/COSTS-platform.md` before the first deploy. The meter while idle is the MLflow
tracking server, the live real-time endpoints, the gateway task and its ALB; `make stop-aws` stops
the server, deletes the live endpoints (an approval brings them back), scales the gateway to
zero and freezes the policy function; tenant serverless endpoints, runtimes, memories and
knowledge bases cost nothing idle. `make destroy-aws` deletes the endpoints, schedules, tenant
runtimes and Studio apps that CloudFormation did not create, then the stack.

The model gateway's ALB is HTTP: TLS needs a domain, so an organisation adds a certificate and
`redirect_http` on the pattern in `gateway.py` and a Route 53 record. Every request already needs a
virtual key.

## Reviewing before a deploy

`tests/test_synth.py` synthesises cohort, solo and staged variants with cdk-nag and checks: KMS
and access logs on every bucket, per-tenant roles that never name another tenant, the approval
event pattern and the rollback alarms, the canary configuration, JWT on every route but the
probes, the prompts against the catalog, the knowledge base and index shape, the AgentCore plane
and registry records, the gateway config and database, the pipeline stages, model access scoped
to the course models, no secret value in the template, zero non-compliant nag rows and no unused
suppression in `stacks/nag.py`.
