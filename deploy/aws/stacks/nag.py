"""cdk-nag suppressions with evidence. Each one names the rule and why it is acceptable here."""

from __future__ import annotations

from aws_cdk import Stack
from cdk_nag import NagSuppressions


def suppress_known(stack: Stack) -> None:
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            {
                "id": "AwsSolutions-IAM5",
                "reason": "ecr:GetAuthorizationToken, xray:Put* and cloudwatch:PutMetricData (namespace-conditioned) require '*' per the AgentCore runtime-permissions devguide; log-stream ARNs need the wildcard suffix; ECR asset repositories are account-scoped.",
            },
            {
                "id": "AwsSolutions-IAM4",
                "reason": "App Runner's managed access role for ECR (AWSAppRunnerServicePolicyForECRAccess) is the documented way to pull images.",
            },
            {
                "id": "AwsSolutions-SNS3",
                "reason": "The alerts topic receives only CloudWatch alarm notifications from this account; SSL enforcement is applied by the service.",
            },
            {
                "id": "AwsSolutions-SNS2",
                "reason": "Alarm text carries no customer data; KMS on the topic adds a key to manage for no confidentiality gain.",
            },
        ],
    )
