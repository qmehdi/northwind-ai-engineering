"""cdk-nag suppressions with evidence. Each one names the rule and why it is acceptable here;
the synth test fails on any finding that is not on this list."""

from __future__ import annotations

from aws_cdk import Stack
from cdk_nag import NagSuppressions


def suppress_known(stack: Stack) -> None:
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            {
                "id": "AwsSolutions-IAM5",
                "reason": (
                    "Wildcards are the documented shape: the AgentCore runtime and evaluation roles "
                    "(devguide runtime-permissions and evaluations-prerequisites: ecr:GetAuthorizationToken, "
                    "xray:Put*, logs on the runtimes log groups, logs:StartQuery on *), the gateway role "
                    "(AuthorizeAction on gateway/* because the gateway ARN is unknown until it exists), the "
                    "SageMaker execution roles (jobs, pipelines, models and endpoints under the owner's "
                    "name prefix, List* actions are account wide, sagemaker-mlflow:* on the one tracking "
                    "server as the MLflow devguide shows), the knowledge base role (s3vectors on every index "
                    "of the one vector bucket, S3 GetObject under tenants/*/policies/*), the approval "
                    "deployer (application-autoscaling has no resource ARNs, endpoints under the prefix), "
                    "the CodeBuild projects and CodePipeline (CDK-managed artifact and log statements), "
                    "S3 grants on prefixes (bucket/prefix/*), the learner roles (Bedrock prompts are "
                    "created on prompt/* with an aws:RequestTag condition because their ids are generated, "
                    "AgentCore CreateAgentRuntime on runtime/* because the runtime id is generated and the "
                    "runtime can only run as the tenant's own role, List and Describe calls that have no "
                    "resource ARNs, record ARNs under the one registry), the Deny statements that guard "
                    "instance types and MLflow deletes (a deny on * is the narrowing), the domain cleanup "
                    "function (EFS and EC2 Describe calls, deletes conditioned on the SageMaker tag and the "
                    "VPC), the Bedrock invocation logging and prefix list lookups (account-level APIs with "
                    "no resource ARNs) and the budget stop policy (a deny on *)."
                ),
            },
            {
                "id": "AwsSolutions-IAM4",
                "reason": (
                    "AWSLambdaBasicExecutionRole is the documented Lambda logging policy, "
                    "AWSXrayWriteOnlyPolicy is the policy AWS documents for the X-Ray OTLP endpoint, "
                    "AWSCodeDeployRoleForLambdaLimited is what the CDK LambdaDeploymentGroup attaches, "
                    "AmazonECSTaskExecutionRolePolicy is the ECS task execution policy, and the "
                    "BucketDeployment and log-retention custom resources use the CDK's own managed policies."
                ),
            },
            {
                "id": "AwsSolutions-SMG4",
                "reason": "Secrets (the API key, the gateway master key, the database password) rotate by redeploying or by the key scripts; a rotation Lambda per secret is out of scope for the course.",
            },
            {
                "id": "AwsSolutions-SNS3",
                "reason": "The alerts topic receives only CloudWatch alarm and CodePipeline approval notifications from this account.",
            },
            {
                "id": "AwsSolutions-L1",
                "reason": "The CDK's own custom-resource providers (BucketDeployment, auto-delete objects, log retention) pin their runtime; the course functions are on Python 3.12.",
            },
            {
                "id": "AwsSolutions-COG8",
                "reason": "The Plus feature plan (threat protection) is billed per monthly active user; the course pool has a handful of users behind required TOTP MFA and a 12-character policy on the Essentials plan.",
            },
            {
                "id": "AwsSolutions-RDS10",
                "reason": "Deletion protection would break `make destroy-aws`; the keys database holds virtual keys that gateway_keys.sh recreates.",
            },
            {
                "id": "AwsSolutions-CFR1",
                "reason": "The model gateway's distribution serves the cohort wherever the learners are; every request needs a virtual key.",
            },
            {
                "id": "AwsSolutions-CFR2",
                "reason": "AWS WAF on the gateway distribution is priced in deploy/COSTS-platform.md and not deployed (a documented course simplification); the ALB answers only CloudFront with the secret origin header, and every request needs a virtual key.",
            },
            {
                "id": "AwsSolutions-CFR4",
                "reason": "The gateway distribution uses CloudFront's default certificate (HTTPS with no domain), whose minimum viewer protocol CloudFront fixes; an organisation with a domain attaches an ACM certificate and sets TLSv1.2_2021 (README, Networking).",
            },
            {
                "id": "AwsSolutions-CFR5",
                "reason": "The CloudFront to ALB leg is HTTP (TLSv1.2 is the only protocol listed for when it moves to HTTPS): an ALB certificate needs a domain the course does not own. The ALB accepts only CloudFront's origin-facing prefix list and the secret X-Origin-Verify header; the README says how an organisation with a domain switches the origin to HTTPS only.",
            },
            {
                "id": "AwsSolutions-ECS2",
                "reason": "The LiteLLM task's environment variables are the config location, the database host and name, and the region; every secret is a Secrets Manager reference.",
            },
            {
                "id": "AwsSolutions-APIG4",
                "reason": "Every route is authorised by the Cognito user pool except GET /healthz and GET /readyz, which are the probes the service layer keeps open on every track.",
            },
        ],
    )
