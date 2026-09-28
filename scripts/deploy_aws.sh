#!/usr/bin/env bash
# AWS: synth, review, deploy. TIER=session|reference. Never deploys behind a failed synth test.
# NW_STAGE=<word> puts the word after `northwind` in every name (stack northwind-<word>-session,
# functions northwind-<word>-triage ...) so dev, staging and prod can share one account.
set -euo pipefail
cd "$(dirname "$0")/.."
TIER="${TIER:-session}"
ACTION="${1:-deploy}"
STAGE="${NW_STAGE:-}"
PREFIX="northwind${STAGE:+-$STAGE}"
SERVICES=(triage semantic policy agent)
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
# The Reference stack's managed retriever is empty after the deploy; fill it from the same
# chunks file the images carry. Idempotent (vectors are upserted by chunk id).
publish_vectors() {
  local bucket
  bucket="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["PolicyVectorBucket"])' deploy/aws/outputs.json "$PREFIX-reference")"
  NW_AWS_REGION="$CDK_DEFAULT_REGION" uv run python -m nw.policy.publish_vectors --bucket "$bucket" --region "$CDK_DEFAULT_REGION"
}
# Progressive delivery from the CLI. `cdk deploy` moves the `live` alias through CodeDeploy on
# its own; this is the same shift for a configuration changed with
# `aws lambda update-function-configuration`, which only touches $LATEST and serves nothing
# until a version is published and the alias moves. publish-version returns the existing
# version when nothing changed, so a repeated call starts no second deployment.
rollout() {
  local fn="$PREFIX-$1" cur new spec id
  aws lambda wait function-updated --function-name "$fn"
  new="$(aws lambda publish-version --function-name "$fn" --query Version --output text)"
  cur="$(aws lambda get-alias --function-name "$fn" --name live --query FunctionVersion --output text)"
  if [ "$cur" = "$new" ]; then echo "$fn: alias live already serves version $new, nothing to roll out"; return; fi
  spec="$(printf '{"version":0.0,"Resources":[{"%s":{"Type":"AWS::Lambda::Function","Properties":{"Name":"%s","Alias":"live","CurrentVersion":"%s","TargetVersion":"%s"}}}]}' "$fn" "$fn" "$cur" "$new")"
  id="$(aws deploy create-deployment --application-name "$PREFIX-lambda" --deployment-group-name "$fn" \
    --revision "$(python3 -c 'import json,sys; print(json.dumps({"revisionType":"AppSpecContent","appSpecContent":{"content":sys.argv[1]}}))' "$spec")" \
    --query deploymentId --output text)"
  echo "$fn: deployment $id moves live from version $cur to $new: 10 percent now, the rest after 15 minutes,"
  echo "back to $cur on its own if $fn-errors or $fn-p95 enters ALARM. Watch it:"
  echo "  aws deploy get-deployment --deployment-id $id --query 'deploymentInfo.[status,deploymentOverview,rollbackInfo]'"
}
deployments() {
  local fn="$PREFIX-$1"
  aws deploy list-deployments --application-name "$PREFIX-lambda" --deployment-group-name "$fn" --query 'deployments[:5]' --output text \
    | tr '\t' '\n' | grep . | while read -r id; do
      aws deploy get-deployment --deployment-id "$id" --query 'deploymentInfo.[deploymentId,status,createTime,rollbackInfo.rollbackMessage]' --output text
    done
}
# The CDK virtualenv and the pinned CDK CLI (deploy/aws/package.json): `make setup-aws` creates both.
if [ ! -x deploy/aws/.venv/bin/python ] || [ ! -x deploy/aws/node_modules/.bin/cdk ]; then
  echo "deploy/aws toolchain missing; running make setup-aws"
  make setup-aws
fi
CTX=(-c "tier=$TIER" -c "stage=$STAGE" -c "alertEmail=${NW_ALERT_EMAIL:-}" -c "budgetUsd=${NW_BUDGET_USD:-100}")
case "$ACTION" in
  rollout|deployments)
    [ -n "${2:-}" ] || { echo "usage: deploy_aws.sh $ACTION triage|semantic|policy|agent"; exit 2; }
    "$ACTION" "$2"; exit 0 ;;
esac
cd deploy/aws
# `npx --yes cdk` runs the aws-cdk version pinned in package.json without a global install.
case "$ACTION" in
  synth)   .venv/bin/python -m pytest -q tests && npx --yes cdk synth "${CTX[@]}" -q ;;
  diff)    npx --yes cdk diff "${CTX[@]}" ;;
  deploy)
    .venv/bin/python -m pytest -q tests
    echo "Read deploy/COSTS.md. Deploying tier=$TIER stage=${STAGE:-default} to account $CDK_DEFAULT_ACCOUNT in $CDK_DEFAULT_REGION."
    echo "The first deploy of a tier creates IAM roles, so cdk asks y/n once before it starts."
    echo "A later deploy that changes a function goes through CodeDeploy: 10 percent for 15 minutes, then the rest."
    npx --yes cdk bootstrap "aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION" -q
    enable_transaction_search
    npx --yes cdk deploy "${CTX[@]}" --require-approval broadening --outputs-file outputs.json
    if [ "$TIER" = "reference" ]; then cd ../.. && publish_vectors; fi
    ;;
  destroy) npx --yes cdk destroy "${CTX[@]}" --force ;;
  stop)
    # Lambda bills nothing while idle. Stop means reserved concurrency 0: every invocation is
    # refused until start removes the limit. Images and the secret keep costing under 1 USD a month.
    for fn in "${SERVICES[@]}"; do
      aws lambda put-function-concurrency --function-name "$PREFIX-$fn" --reserved-concurrent-executions 0 >/dev/null && echo "stopped $PREFIX-$fn"; done ;;
  start)
    for fn in "${SERVICES[@]}"; do
      aws lambda delete-function-concurrency --function-name "$PREFIX-$fn" && echo "started $PREFIX-$fn"; done ;;
  *) echo "usage: deploy_aws.sh synth|diff|deploy|stop|start|destroy|rollout <service>|deployments <service>"; exit 2 ;;
esac
