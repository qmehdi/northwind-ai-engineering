#!/usr/bin/env bash
# AWS: synth, review, deploy. TIER=session|reference. Never deploys behind a failed synth test.
set -euo pipefail
cd "$(dirname "$0")/.."
TIER="${TIER:-session}"
ACTION="${1:-deploy}"
export JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION=1
export CDK_DEFAULT_ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
export CDK_DEFAULT_REGION="${CDK_DEFAULT_REGION:-${NW_AWS_REGION:-us-east-1}}"
# Spans from the services go to the X-Ray OTLP endpoint, which needs Transaction Search on for
# the account (docs: CloudWatch-Transaction-Search-getting-started). Idempotent.
enable_transaction_search() {
  if [ "$(aws xray get-trace-segment-destination --query Destination --output text 2>/dev/null)" = "CloudWatchLogs" ]; then return; fi
  echo "Enabling CloudWatch Transaction Search for account $CDK_DEFAULT_ACCOUNT"
  aws logs put-resource-policy --policy-name NorthwindTransactionSearch --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"TransactionSearchXRayAccess\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"xray.amazonaws.com\"},\"Action\":\"logs:PutLogEvents\",\"Resource\":[\"arn:aws:logs:$CDK_DEFAULT_REGION:$CDK_DEFAULT_ACCOUNT:log-group:aws/spans:*\",\"arn:aws:logs:$CDK_DEFAULT_REGION:$CDK_DEFAULT_ACCOUNT:log-group:/aws/application-signals/data:*\"],\"Condition\":{\"ArnLike\":{\"aws:SourceArn\":\"arn:aws:xray:$CDK_DEFAULT_REGION:$CDK_DEFAULT_ACCOUNT:*\"},\"StringEquals\":{\"aws:SourceAccount\":\"$CDK_DEFAULT_ACCOUNT\"}}}]}" >/dev/null
  aws xray update-trace-segment-destination --destination CloudWatchLogs >/dev/null
  aws xray update-indexing-rule --name Default --rule '{"Probabilistic": {"DesiredSamplingPercentage": 1}}' >/dev/null
}
CTX=(-c "tier=$TIER" -c "alertEmail=${NW_ALERT_EMAIL:-}" -c "budgetUsd=${NW_BUDGET_USD:-100}")
cd deploy/aws
case "$ACTION" in
  synth)   .venv/bin/python -m pytest -q tests && npx cdk synth "${CTX[@]}" -q ;;
  diff)    npx cdk diff "${CTX[@]}" ;;
  deploy)
    .venv/bin/python -m pytest -q tests
    echo "Read deploy/COSTS.md. Deploying tier=$TIER to account $CDK_DEFAULT_ACCOUNT in $CDK_DEFAULT_REGION."
    npx cdk bootstrap "aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION" -q
    enable_transaction_search
    npx cdk deploy "${CTX[@]}" --require-approval broadening --outputs-file outputs.json
    ;;
  destroy) npx cdk destroy "${CTX[@]}" --force ;;
  stop)
    # Lambda bills nothing while idle. Stop means reserved concurrency 0: every invocation is
    # refused until start removes the limit. Images and the secret keep costing under 1 USD a month.
    for fn in triage semantic policy agent; do
      aws lambda put-function-concurrency --function-name "northwind-$fn" --reserved-concurrent-executions 0 >/dev/null && echo "stopped northwind-$fn"; done ;;
  start)
    for fn in triage semantic policy agent; do
      aws lambda delete-function-concurrency --function-name "northwind-$fn" && echo "started northwind-$fn"; done ;;
  *) echo "usage: deploy_aws.sh synth|diff|deploy|stop|start|destroy"; exit 2 ;;
esac
