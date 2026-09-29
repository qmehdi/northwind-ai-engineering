#!/usr/bin/env bash
# AWS track: one platform stack per environment (ADR 0008, 0009). Never deploys behind a failed
# synth test. NW_ENV=<word> puts the word after `northwind` in every name (stack
# northwind-<word>-platform) so a second environment is a second deploy of the same code.
#
#   scripts/deploy_aws.sh synth                 # synth test and cdk synth
#   scripts/deploy_aws.sh deploy                # bootstrap, Transaction Search, cdk deploy, keys, corpus
#   scripts/deploy_aws.sh tenants add alice     # add a tenant (redeploy) and mint their key
#   scripts/deploy_aws.sh tenants remove alice  # remove a tenant (redeploy)
#   scripts/deploy_aws.sh tenants               # list the tenants of the deployed platform
#   scripts/deploy_aws.sh status                # what is up: endpoints, runtimes, gateway, MLflow
#   scripts/deploy_aws.sh stop | start          # idle cost to the floor and back
#   scripts/deploy_aws.sh destroy               # everything, endpoints and runtimes first
#
# Mode: NW_MODE=solo (one tenant named solo, the default) or NW_TENANTS=alice,bob (cohort).
# The GitHub connection is a manual handshake (README, "Prerequisites"); pass its ARN as
# NW_CONNECTION_ARN to get the delivery pipeline.
set -euo pipefail
cd "$(dirname "$0")/.."
ACTION="${1:-deploy}"
ENV_NAME="${NW_ENV:-}"
PREFIX="northwind${ENV_NAME:+-$ENV_NAME}"
STACK="${PREFIX}-platform"
UNDER="${PREFIX//-/_}"
OUT=deploy/aws/outputs.json
export JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION=1
export CDK_DEFAULT_ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
export CDK_DEFAULT_REGION="${CDK_DEFAULT_REGION:-${NW_AWS_REGION:-us-east-1}}"

output() { python3 -c 'import json,sys; o=json.load(open(sys.argv[1])); o=next(v for k,v in o.items() if k.startswith("northwind")); print(o.get(sys.argv[2],""))' "$OUT" "$1"; }
tenants_deployed() { [ -f "$OUT" ] && output Tenants || echo "${NW_TENANTS:-}"; }

# Spans go to the X-Ray OTLP endpoint, which needs Transaction Search on for the account
# (docs: CloudWatch-Transaction-Search-getting-started). Idempotent.
enable_transaction_search() {
  if [ "$(aws xray get-trace-segment-destination --query Destination --output text 2>/dev/null)" = "CloudWatchLogs" ]; then return; fi
  echo "Enabling CloudWatch Transaction Search for account $CDK_DEFAULT_ACCOUNT"
  aws logs put-resource-policy --policy-name NorthwindTransactionSearch --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"TransactionSearchXRayAccess\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"xray.amazonaws.com\"},\"Action\":\"logs:PutLogEvents\",\"Resource\":[\"arn:aws:logs:$CDK_DEFAULT_REGION:$CDK_DEFAULT_ACCOUNT:log-group:aws/spans:*\",\"arn:aws:logs:$CDK_DEFAULT_REGION:$CDK_DEFAULT_ACCOUNT:log-group:/aws/application-signals/data:*\"],\"Condition\":{\"ArnLike\":{\"aws:SourceArn\":\"arn:aws:xray:$CDK_DEFAULT_REGION:$CDK_DEFAULT_ACCOUNT:*\"},\"StringEquals\":{\"aws:SourceAccount\":\"$CDK_DEFAULT_ACCOUNT\"}}}]}" >/dev/null
  aws xray update-trace-segment-destination --destination CloudWatchLogs >/dev/null
  aws xray update-indexing-rule --name Default --rule '{"Probabilistic": {"DesiredSamplingPercentage": 1}}' >/dev/null
}
# The knowledge bases are empty after the deploy: copy the policy corpus under every owner's
# prefix and start one ingestion job each. Idempotent (the data source syncs by key).
publish_corpus() {
  local bucket owner kb ds
  bucket="$(output DataBucket)"
  aws s3 cp data/tickets.jsonl "s3://$bucket/data/tickets/tickets.jsonl" >/dev/null
  for owner in $(tr ',' ' ' <<<"$(output Tenants)") live; do
    aws s3 sync data/policies "s3://$bucket/tenants/$owner/policies/" --quiet
    kb="$(output KnowledgeBases | tr ',' '\n' | grep "^$owner=" | cut -d= -f2)"
    ds="$(aws bedrock-agent list-data-sources --knowledge-base-id "$kb" --query 'dataSourceSummaries[0].dataSourceId' --output text)"
    aws bedrock-agent start-ingestion-job --knowledge-base-id "$kb" --data-source-id "$ds" --query 'ingestionJob.[ingestionJobId,status]' --output text
  done
}
mint_keys() {
  local owner
  for owner in $(tr ',' ' ' <<<"$(output Tenants)") live; do deploy/aws/scripts/gateway_keys.sh "$owner"; done
}
# The CDK virtualenv and the pinned CDK CLI (deploy/aws/package.json): `make setup-aws` creates both.
if [ ! -x deploy/aws/.venv/bin/python ] || [ ! -x deploy/aws/node_modules/.bin/cdk ]; then
  echo "deploy/aws toolchain missing; running make setup-aws"
  make setup-aws
fi
context() {
  local tenants="${1:-${NW_TENANTS:-}}"
  CTX=(-c "env=$ENV_NAME" -c "alertEmail=${NW_ALERT_EMAIL:-}" -c "budgetUsd=${NW_BUDGET_USD:-200}" -c "connectionArn=${NW_CONNECTION_ARN:-}" -c "lakeFormation=${NW_LAKE_FORMATION:-false}")
  if [ -n "$tenants" ]; then CTX+=(-c "tenants=$tenants"); else CTX+=(-c "mode=${NW_MODE:-solo}"); fi
}
cdk_deploy() {
  uv run python -m nw.platform.aws prompts-catalog >/dev/null
  (cd deploy/aws && .venv/bin/python -m pytest -q tests)
  echo "Read deploy/COSTS-platform.md. Deploying $STACK to account $CDK_DEFAULT_ACCOUNT in $CDK_DEFAULT_REGION."
  echo "The first deploy creates IAM roles, so cdk asks y/n once; the SageMaker domain and the database take about 15 minutes."
  (cd deploy/aws && npx --yes cdk bootstrap "aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION" -q && npx --yes cdk deploy "${CTX[@]}" --require-approval broadening --outputs-file outputs.json)
}
case "$ACTION" in
  synth)
    context; uv run python -m nw.platform.aws prompts-catalog >/dev/null
    (cd deploy/aws && .venv/bin/python -m pytest -q tests && npx --yes cdk synth "${CTX[@]}" -q) ;;
  diff)    context; (cd deploy/aws && npx --yes cdk diff "${CTX[@]}") ;;
  deploy)
    context; enable_transaction_search; cdk_deploy; mint_keys; publish_corpus
    if [ -z "${NW_CONNECTION_ARN:-}" ]; then echo "No delivery pipeline (NW_CONNECTION_ARN unset): push the images, the pipelines image included, with make images-aws."; fi
    echo "Next: scripts/deploy_aws.sh status; the README's tenant workflow says how a learner gets their key." ;;
  tenants)
    sub="${2:-list}"; name="${3:-}"
    current="$(tenants_deployed)"
    case "$sub" in
      list) echo "${current:-none}" ;;
      add)
        [ -n "$name" ] || { echo "usage: deploy_aws.sh tenants add <name>"; exit 2; }
        new="$(python3 -c 'import sys; cur=[t for t in sys.argv[1].split(",") if t]; cur+=[sys.argv[2]] if sys.argv[2] not in cur else []; print(",".join(cur))' "$current" "$name")"
        context "$new"; cdk_deploy; deploy/aws/scripts/gateway_keys.sh "$name"
        bucket="$(output DataBucket)"; aws s3 sync data/policies "s3://$bucket/tenants/$name/policies/" --quiet
        kb="$(output KnowledgeBases | tr ',' '\n' | grep "^$name=" | cut -d= -f2)"
        ds="$(aws bedrock-agent list-data-sources --knowledge-base-id "$kb" --query 'dataSourceSummaries[0].dataSourceId' --output text)"
        aws bedrock-agent start-ingestion-job --knowledge-base-id "$kb" --data-source-id "$ds" >/dev/null
        echo "tenant $name added: profile northwind-$name, key in ${PREFIX}-$name-gateway-key, knowledge base $kb" ;;
      remove)
        [ -n "$name" ] || { echo "usage: deploy_aws.sh tenants remove <name>"; exit 2; }
        new="$(python3 -c 'import sys; print(",".join(t for t in sys.argv[1].split(",") if t and t != sys.argv[2]))' "$current" "$name")"
        for ep in $(aws sagemaker list-endpoints --name-contains "$PREFIX-$name-" --query 'Endpoints[].EndpointName' --output text); do
          aws sagemaker delete-endpoint --endpoint-name "$ep" && echo "deleted endpoint $ep"; done
        rt="$(aws bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[?agentRuntimeName=='${UNDER}_${name}_resolver'].agentRuntimeId" --output text)"
        [ -n "$rt" ] && [ "$rt" != "None" ] && aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$rt" >/dev/null && echo "deleted runtime ${UNDER}_${name}_resolver"
        aws secretsmanager delete-secret --secret-id "$PREFIX-$name-gateway-key" --force-delete-without-recovery >/dev/null 2>&1 || true
        context "$new"; cdk_deploy
        echo "tenant $name removed" ;;
      *) echo "usage: deploy_aws.sh tenants [list|add <name>|remove <name>]"; exit 2 ;;
    esac ;;
  status)
    echo "stack $STACK in $CDK_DEFAULT_REGION"
    aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text
    echo "tenants: $(tenants_deployed)"
    echo "endpoints:"; aws sagemaker list-endpoints --name-contains "$PREFIX-" --query 'Endpoints[].[EndpointName,EndpointStatus]' --output text | sed 's/^/  /'
    echo "runtimes:"; aws bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[?starts_with(agentRuntimeName,'${UNDER}_')].[agentRuntimeName,status]" --output text | sed 's/^/  /'
    echo "mlflow: $(aws sagemaker describe-mlflow-tracking-server --tracking-server-name "$PREFIX-mlflow" --query '[TrackingServerStatus,IsActive]' --output text)"
    echo "gateway: $(aws ecs describe-services --cluster "$PREFIX-gateway" --services "$PREFIX-gateway" --query 'services[0].[status,runningCount,desiredCount]' --output text) at $(output GatewayUrl)"
    echo "policy api: $(output UrlPolicy)" ;;
  stop)
    # Idle cost to the floor: tenant serverless endpoints cost nothing idle and stay; the live
    # real-time endpoints, the MLflow server and the gateway task are the meter, so they stop.
    for ep in $(aws sagemaker list-endpoints --name-contains "$PREFIX-live-" --query 'Endpoints[].EndpointName' --output text); do
      aws sagemaker delete-endpoint --endpoint-name "$ep" && echo "deleted live endpoint $ep (re-approve a package to bring it back)"; done
    aws sagemaker stop-mlflow-tracking-server --tracking-server-name "$PREFIX-mlflow" >/dev/null && echo "stopped $PREFIX-mlflow"
    aws ecs update-service --cluster "$PREFIX-gateway" --service "$PREFIX-gateway" --desired-count 0 >/dev/null && echo "stopped $PREFIX-gateway (desired 0)"
    aws lambda put-function-concurrency --function-name "$PREFIX-policy" --reserved-concurrent-executions 0 >/dev/null && echo "stopped $PREFIX-policy" ;;
  start)
    aws sagemaker start-mlflow-tracking-server --tracking-server-name "$PREFIX-mlflow" >/dev/null && echo "started $PREFIX-mlflow"
    aws ecs update-service --cluster "$PREFIX-gateway" --service "$PREFIX-gateway" --desired-count 1 >/dev/null && echo "started $PREFIX-gateway"
    aws lambda delete-function-concurrency --function-name "$PREFIX-policy" && echo "started $PREFIX-policy"
    echo "live endpoints return on the next approved package: scripts/deploy_aws.sh status" ;;
  release)
    # Start the delivery pipeline on the current commit (a push to main does the same);
    # it builds, pushes by digest and stops at the Approve stage.
    aws codepipeline start-pipeline-execution --name "$PREFIX-delivery" --query pipelineExecutionId --output text ;;
  approve)
    # Approve the Promote action waiting in the Approve stage, with a reason for the record.
    token="$(aws codepipeline get-pipeline-state --name "$PREFIX-delivery" \
      --query "stageStates[?stageName=='Approve'].actionStates[0].latestExecution.token" --output text)"
    if [ -z "$token" ] || [ "$token" = "None" ]; then echo "nothing is waiting for approval"; exit 1; fi
    aws codepipeline put-approval-result --pipeline-name "$PREFIX-delivery" --stage-name Approve --action-name Promote \
      --token "$token" --result "summary=${REASON:-approved from the course},status=Approved" >/dev/null
    echo "approved; the deploy stage shifts the canary next" ;;
  destroy)
    context "$(tenants_deployed)"
    # Endpoints, monitoring schedules and tenant runtimes were created by the approval Lambda
    # and the client, not by CloudFormation, so they go first.
    for ms in $(aws sagemaker list-monitoring-schedules --name-contains "$PREFIX-" --query 'MonitoringScheduleSummaries[].MonitoringScheduleName' --output text); do
      aws sagemaker delete-monitoring-schedule --monitoring-schedule-name "$ms" && echo "deleted schedule $ms"; done
    for ep in $(aws sagemaker list-endpoints --name-contains "$PREFIX-" --query 'Endpoints[].EndpointName' --output text); do
      aws sagemaker delete-endpoint --endpoint-name "$ep" && echo "deleted endpoint $ep"; done
    for rt in $(aws bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[?starts_with(agentRuntimeName,'${UNDER}_') && !contains(agentRuntimeName,'_live_') && !contains(agentRuntimeName,'_tools')].agentRuntimeId" --output text); do
      aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$rt" >/dev/null && echo "deleted tenant runtime $rt"; done
    # Studio apps and spaces block the domain's deletion; the profiles' apps go with them.
    domain="$(output DomainId)"
    if [ -n "$domain" ]; then
      for app in $(aws sagemaker list-apps --domain-id-equals "$domain" --query 'Apps[?Status!=`Deleted`].[UserProfileName,AppType,AppName]' --output text | tr '\t' ','); do
        IFS=, read -r up at an <<<"$app"; aws sagemaker delete-app --domain-id "$domain" --user-profile-name "$up" --app-type "$at" --app-name "$an" 2>/dev/null || true; done
    fi
    (cd deploy/aws && npx --yes cdk destroy "${CTX[@]}" --force) ;;
  *) echo "usage: deploy_aws.sh synth|diff|deploy|tenants [list|add|remove <name>]|status|stop|start|destroy"; exit 2 ;;
esac
