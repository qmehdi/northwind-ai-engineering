# deploy/aws

CDK (Python) for the AWS track: one platform stack per environment (ADR 0005, 0008, 0009, 0012),
`northwind-platform` by default, `northwind-<env>-platform` with `NW_ENV=<env>`. The stack is
`stacks/platform.py`; each area of the reference architectures is a construct in `stacks/areas/`.

| Area | Construct | What it deploys |
| --- | --- | --- |
| Network | `network.py` | The platform VPC (public subnets, no NAT); the agents network: two private subnets placed by AgentCore AZ ID, one NAT gateway, an HTTPS-only security group, a DNS Firewall allow-list, an S3 gateway endpoint |
| Data and governance | `data.py` | KMS key; `data`, `artifacts`, `ops` and access-logs buckets (KMS, versioned, SSL only) with lifecycle rules for the retention classes; the `ops` bucket is `NW_OPS_STORE` (trajectories, feedback, approvals per owner prefix); Glue database and the `tickets` table; Lake Formation registration off by default |
| Tracking and registry | `tracking.py` | SageMaker domain (IAM), a user profile and an execution role per tenant scoped to `northwind-<tenant>-*` (course instance types only, no MLflow deletes), the managed MLflow tracking server, a Model Package Group per project and tenant, the domain cleanup on destroy |
| Tenants | `tenants.py` | The learner role per tenant (`northwind-<tenant>-learner`, ABAC on `nw:tenant`): the documented tenant workflow and nothing of another tenant's |
| Pipelines | `pipelines.py` | A weekly EventBridge schedule per tenant that starts `northwind-<tenant>-triage`, disabled until enabled on purpose |
| Serving | `serving.py`, `functions/deploy_on_approval` | The approval-driven deployer (validates images and data, serverless endpoint for a tenant, canary, autoscaling and Model Monitor for `live`), one serving role per owner, the live endpoints' alarms including quality, the policy service as a Lambda behind an HTTP API with Cognito, CodeDeploy canary on its alias |
| Prompts and retrieval | `prompts.py` | Bedrock prompts from `prompts/catalog.json` with a version each; an S3 Vectors bucket, one index and one Knowledge Base per tenant and for `live` |
| Agents | `agents.py` | AgentCore Runtime (tools as MCP, the live resolver) in VPC mode, one runtime execution role per tenant, Gateway with a Cedar policy engine and the approvers role, Memory per tenant, Identity (workload identity, API key credential provider), per-tenant API keys, Evaluations (custom judge, online config), the AWS Agent Registry with the tools and resolver records, the guardrail |
| Model gateway | `gateway.py` | LiteLLM (pinned by digest) on ECS Fargate behind an ALB that answers only CloudFront, CloudFront for HTTPS, Aurora Serverless v2 for virtual keys, the master, salt and origin keys and one virtual key secret per owner in Secrets Manager, an application inference profile per tenant and role, the EU residency route |
| Delivery | `delivery.py`, `delivery/deploy.sh` | Tag-immutable ECR repositories, CodePipeline V2 from GitHub (CodeConnections), CodeBuild on arm64, images signed with AWS Signer (Notation), manual approval, signatures verified, deploy by digest with the CodeDeploy canary, the promoted digests in SSM, the `northwind-deployer` role |
| Identity and observability | `identity.py` | Cognito user pool for the web entry, CloudTrail with its own key, Bedrock invocation logging (metadata only), the platform dashboard, the alerts topic, the budget with a stop action at 100 percent |

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
`-c agentEgress=vpc|public` (default `vpc`, see "Networking"); `-c githubOwner`,
`-c githubRepo`, `-c githubBranch`. `scripts/deploy_aws.sh` maps them from `NW_TENANTS`,
`NW_MODE`, `NW_ENV`, `NW_ALERT_EMAIL`, `NW_BUDGET_USD`, `NW_CONNECTION_ARN`,
`NW_LAKE_FORMATION` and `NW_AGENT_EGRESS`. Tenant names are 2 to 16 lowercase letters or digits;
platform words (`live`, `platform`, `gateway`, `mlflow`, ...) and environment words (`dev`,
`staging`, `prod`, ...) are refused, so a tenant can never collide with an environment's names.

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
  still serves, so treat the drift alarm as a reference-only panel there (its
  `-monitor-silent` companion then fires, which is the honest signal).
- The region must list AgentCore VPC Availability Zones (us-east-1 does: `use1-az1`, `use1-az2`,
  `use1-az4`); elsewhere pass `NW_AGENT_EGRESS=public`.
- A Model Monitor schedule needs a baseline: the live promotion copies
  `baseline/statistics.json` and `constraints.json` from beside the promoted `model.tar.gz`
  (`nw.serving.sagemaker.baseline` writes them) into `monitoring/baselines/<endpoint>/`.

`make deploy-aws` runs the synth review, creates the image parameters (`/northwind/images/policy`
and `agent`, value `asset`) if missing, deploys, mints a model gateway virtual key per tenant and
for `live` (`scripts/gateway_keys.sh`, written into the stack-owned secret
`northwind-<owner>-gateway-key`), uploads the tickets and the policy corpus under every owner's
prefix and starts one ingestion job per knowledge base. It writes `outputs.json` here; `nw/platform/aws.py` reads it, and `outputs.json`
plus `.env` are the two files a learner needs.

## The tenant workflow

In cohort mode the instructor owns the platform and each learner is a tenant named after their
handle. What a tenant gets, all named `northwind-<tenant>-...`:

| Resource | Name | Made by |
| --- | --- | --- |
| Learner role (the identity the learner uses) | `northwind-<tenant>-learner` | the stack |
| Studio profile and execution role | `northwind-<tenant>`, `northwind-<tenant>-sagemaker` | the stack |
| Serving role and AgentCore runtime role | `northwind-<tenant>-serving`, `northwind-<tenant>-agentcore` | the stack |
| Service API key | secret `northwind-<tenant>-api-key` | the stack |
| Model Package Groups | `northwind-<tenant>-triage`, `-semantic` | the stack |
| Pipelines | `northwind-<tenant>-triage`, `-semantic` | the tenant, `nw/pipelines` through the execution role |
| Retraining schedules | `northwind-<tenant>-retrain-triage`, `-retrain-semantic` (disabled, `Trigger=schedule`) | the stack; the gates read `s3://<artifacts>/baselines/*_production.json`, also from the stack |
| Serverless endpoints | `northwind-<tenant>-triage`, `-semantic` | the approval Lambda when a package is approved |
| Knowledge base and S3 Vectors index | `northwind-<tenant>-policies`, `<tenant>-policies` | the stack; filled by `VectorStore.upsert` |
| Prompts | `northwind-<tenant>-<name>` | the tenant through `PromptStore` |
| Inference profiles and gateway key | `northwind-<tenant>-{workhorse,judge,economy}`, secret `northwind-<tenant>-gateway-key` | the stack; the key's value by `gateway_keys.sh` |
| AgentCore Memory | `northwind_<tenant>_memory` | the stack |
| Resolver runtime and registry record | `northwind_<tenant>_resolver` (runs as `northwind-<tenant>-agentcore`, VPC mode on `AgentSubnets` and `AgentSecurityGroup`), record `northwind-<tenant>-resolver` | the tenant through `AgentRuntime.deploy` and `register` |

The learner's `.env`: `NW_TRACK=aws`, `NW_TENANT=<handle>`, `NW_ENVIRONMENT=northwind`,
`NW_GATEWAY_URL=<GatewayUrl output, https>`, `NW_GATEWAY_KEY=<their virtual key>`, and an AWS
profile that assumes `northwind-<handle>-learner` (see "Learner access"); with that role the
learner reads their own key and API key from Secrets Manager themselves.

### Learner access

The instructor never hands out administrator access. Each learner gets an identity in the
account (an IAM Identity Center user with the attribute `nw:tenant=<handle>` mapped as an
attribute for access control, or an IAM user tagged `nw:tenant=<handle>`) whose only
permission is `sts:AssumeRole` on `northwind-*-learner`; the learner role's trust policy admits
the caller only when its `nw:tenant` tag equals the role's tenant. The learner's AWS config:

```ini
[profile northwind]
role_arn = arn:aws:iam::<account>:role/northwind-<handle>-learner
source_profile = <their identity>
```

What the role can do is the tenant workflow and nothing more: SageMaker pipelines, jobs,
models, endpoints and packages under `northwind-<handle>-*` (plus the promotion into the live
package group), Studio through their own user profile, MLflow without deletes, `tenants/<handle>/`
in both buckets, prompts tagged `nw:tenant=<handle>`, their knowledge base and vector index,
their three inference profiles, AgentCore runtimes named `northwind_<handle>_*` running as their
own runtime role, their memory, Agent Registry records (create, update, submit; approving and
deleting are denied), and their two secrets. Compute is limited to `ml.t3.medium`,
`ml.m5.large`, `ml.m5.xlarge` and `ml.m5.2xlarge` with no accelerators, on the learner role and on
every tenant execution role. At 100 percent of the monthly budget a Budgets action attaches
`northwind-budget-stop` (a deny on new SageMaker, Bedrock and AgentCore spend) to every learner,
execution and tenant runtime role; the instructor detaches it to resume. The approvers role
(`NorthwindApprovers`) is the only principal the Cedar policy lets call `escalate` and the
registry's curator; the instructor assumes it.
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
   the rest, rolled back on `northwind-live-triage-5xx`, `-p95` or the quality level alarm
   `northwind-triage-quality-level`. The `-drift` alarm and the other quality alarms page the
   alerts topic and roll nothing back: an hourly Model Monitor job cannot judge a five-minute
   canary. CodeDeploy has no
   SageMaker platform; the endpoint canary is SageMaker's own deployment guardrail
   (`DeploymentConfig.BlueGreenUpdatePolicy`), driven by that function. The first live deploy has
   no blue fleet: it creates the endpoint with data capture, a scaling target and the Model
   Monitor schedule.
4. Code changes take the other road: a push to `main` starts `northwind-delivery`, CodeBuild
   builds both images on arm64, pushes them by digest to tag-immutable repositories and signs
   each digest with AWS Signer, the `Approve` stage waits for a person (the alerts topic is
   notified), and `delivery/deploy.sh` verifies both signatures, moves the policy Lambda's `live`
   alias through CodeDeploy (10 percent for 15 minutes, rollback on its errors, p95 or quality
   level alarm) and updates the live resolver runtime to the new digest, passing its whole
   configuration back. The deploy stage assumes `northwind-deployer` first: the same STS hop a
   lower environment makes into a higher one. After each step it writes the digest to
   `/northwind/images/policy` or `agent`, the parameters the stack reads for those images, so a
   later `cdk deploy` keeps what was promoted instead of rolling it back.
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

## Networking

- **Model gateway**: `https://<distribution>.cloudfront.net` with CloudFront's default
  certificate (no domain needed). The ALB's security group admits only CloudFront's
  origin-facing managed prefix list, and the listener forwards only requests carrying the secret
  `X-Origin-Verify` header CloudFront adds (the value is a Secrets Manager secret); anything else
  gets 403. The CloudFront to ALB leg is HTTP inside AWS's network. An organisation with a
  domain adds an ACM certificate and an HTTPS listener to the ALB, sets the origin to HTTPS only
  and attaches its own certificate to the distribution with TLSv1.2_2021; WAF on the
  distribution is priced in `deploy/COSTS-platform.md`. Every request also needs a virtual key.
- **Agents**: the runtimes run in VPC mode in the agents network: private subnets in AgentCore's
  listed Availability Zones, one NAT gateway, a security group that allows TCP 443 out and
  nothing else, and a Route 53 Resolver DNS Firewall that answers NXDOMAIN for any name outside
  `amazonaws.com`, `*.amazonaws.com` and the gateway's CloudFront name. A tool or a prompt
  injection that tries to reach another host fails at name resolution. Not covered: a
  connection to a literal IP address on 443; AWS Network Firewall with SNI rules closes that
  and is priced, not deployed. `NW_AGENT_EGRESS=public` drops the agents network (and its NAT
  gateway's idle cost) and runs the runtimes in PUBLIC mode, without egress control.
- **Everything else**: public endpoints behind keys (the policy API behind Cognito, SageMaker and
  AgentCore behind IAM), a documented course simplification.

## Retention

Lifecycle rules and log group retention implement the retention classes
(`docs/governance`): Operational 90 days (`capture/`, `traces/`, `monitoring/`, `mlflow/` in the
artifacts bucket, `<environment>-<owner>/trajectories/` and `/feedback/` in the ops bucket, the
pipeline artifacts bucket, and `aws/spans`, which `deploy_aws.sh` sets because Transaction Search
creates it); Audit 400 days (`<environment>-<owner>/approvals/` in the ops bucket, which holds the
claim markers, approval records and the escalation queue, S3 access logs, ALB and CloudFront logs,
the trail and its log group, Bedrock invocation metadata); application and runtime logs 30 days; AgentCore Memory
events 30 days (`EventExpiryDuration`), with semantic records erased by namespace through
`DeleteMemoryRecord` because the resource has no expiry for them; superseded object versions 30
days; database backups 7 days.

## Operations state and the approval gate

The agents and the policy service keep their durable state in the ops bucket (`NW_OPS_STORE`,
output `OpsStore`, `nw/agent/opstore.py`): `<environment>-<owner>/trajectories/` (every run, with
its proposed actions), `/feedback/` (policy verdicts) and `/approvals/` (claim markers, approval
records, the escalation queue). A runtime role (the live and tools runtimes, a tenant's
`northwind-<tenant>-agentcore`, the policy Lambda) reads and writes `trajectories/` and
`feedback/` of its own owner and nothing else; the learner role (for its tenant) and the approvers
role (for `live`) also write `approvals/`. That is the data-plane half of the approval gate
(ADR 0005) beside the Cedar policy on the tools gateway: an agent that got past the loop's gate
could still not queue an escalation. An approver runs `make approve` with `NW_OPS_STORE`,
`NW_ENVIRONMENT` and `NW_TENANT` set (`NW_TENANT=live` for the live runtime).

## Costs, stop and destroy

Read `deploy/COSTS-platform.md` before the first deploy. The meters while idle are the MLflow
tracking server, the two live real-time endpoints and their hourly Model Monitor jobs, the
gateway task, its ALB and the keys database (Aurora Serverless v2 stays at 0.5 ACU while the
gateway holds connections), the agents' NAT gateway and four public IPv4 addresses. `make
stop-aws` stops the server, deletes the live endpoints (an approval brings them back), scales the
gateway to zero (the database then pauses) and freezes the policy function; the NAT gateway,
the ALB and three addresses remain. Tenant serverless endpoints, runtimes, memories and
knowledge bases cost nothing idle. `make destroy-aws` deletes the endpoints, schedules, data
quality job definitions, tenant runtimes and Studio apps and spaces that CloudFormation did not
create, then the stack (whose `DomainCleanup` custom resource removes the domain's home EFS and
its NFS security groups so the VPC can go), then the image parameters. Gateway keys are
stack-owned secrets and go with it.

## Reviewing before a deploy

`tests/test_synth.py` synthesises cohort, solo and staged variants with cdk-nag and checks: KMS
and access logs on every bucket, the CloudTrail key policy (without it CreateTrail rolls the
stack back and nothing else catches it), per-tenant learner, execution, serving and runtime roles
that never name another tenant, the instance type guard, the deployer's allow-list (the handler
is unit tested with foreign images and paths), the exact approval event pattern and the rollback
alarms, the canary configuration, JWT on every route but the probes, the prompts against the
catalog, the knowledge base and index shape, the AgentCore plane in VPC mode behind the DNS
allow-list, the Cedar permit and the approvers role, the gateway behind CloudFront with the
prefix list and the origin header, the image pinned by digest, the model ids against
`nw/config.py` and the Judge on its geo profile, the lifecycle rules, the image parameters and
the full-configuration runtime update, signing and immutable tags, the budget stop action, the
domain cleanup ordering, model access scoped to the course models, no secret value in the
template, reserved tenant names refused, zero non-compliant nag rows and no unused suppression
in `stacks/nag.py`.

## Running the tests on the AWS track

`make test` also runs the Google Cloud and Azure platform tests, which depend on those tracks'
tool versions (a newer Terraform, for example, breaks `tests/platform/test_gcp_terraform.py`).
On the AWS track, run the same list without them:

```bash
AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
  uv run pytest -q @tests/skeleton-green.txt -k "not gcp_terraform and not azure_bicep"
```

The course's tests never need an AWS account. Hiding the AWS config matters if your default
profile uses `aws login` (a `login_session` profile): boto3 then needs `botocore[crt]`, which
the course does not install, and two `session01` provider tests fail.

The stack's own tests run before every `make synth-aws` and `make deploy-aws`; to run them alone:

```bash
(cd deploy/aws && CDK_DEFAULT_ACCOUNT=123456789012 .venv/bin/python -m pytest -q tests)
```
