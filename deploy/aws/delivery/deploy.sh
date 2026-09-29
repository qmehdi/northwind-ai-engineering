#!/usr/bin/env bash
# The Deploy stage of the delivery pipeline. Runs in CodeBuild after the manual approval, with
# the image digests the Build stage wrote to images.json. Promotes by digest, never by tag:
#   1. the policy Lambda: update the code to the digest, publish a version, shift the `live`
#      alias through CodeDeploy (10 percent for 15 minutes, rollback on the alarms);
#   2. the live resolver runtime: update the AgentCore Runtime to the digest (AgentCore keeps
#      the previous version; a session in flight finishes on it).
# When DEPLOYER_ROLE_ARN is set the script assumes it first: that is the STS hop a pipeline in
# a lower environment makes into a higher one (README, "Lower and higher environments").
set -euo pipefail
IMAGES="${IMAGES_JSON:-$CODEBUILD_SRC_DIR_build/images.json}"
POLICY_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["policy"])' "$IMAGES")"
AGENT_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["agent"])' "$IMAGES")"

if [ -n "${DEPLOYER_ROLE_ARN:-}" ]; then
  creds="$(aws sts assume-role --role-arn "$DEPLOYER_ROLE_ARN" --role-session-name delivery --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)"
  read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN <<<"$creds"
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
  echo "assumed $DEPLOYER_ROLE_ARN"
fi

echo "policy: $POLICY_DIGEST"
aws lambda update-function-code --function-name "$POLICY_FUNCTION" --image-uri "$POLICY_DIGEST" >/dev/null
aws lambda wait function-updated --function-name "$POLICY_FUNCTION"
new="$(aws lambda publish-version --function-name "$POLICY_FUNCTION" --query Version --output text)"
cur="$(aws lambda get-alias --function-name "$POLICY_FUNCTION" --name live --query FunctionVersion --output text)"
if [ "$cur" = "$new" ]; then
  echo "alias live already serves version $new"
else
  spec="$(printf '{"version":0.0,"Resources":[{"%s":{"Type":"AWS::Lambda::Function","Properties":{"Name":"%s","Alias":"live","CurrentVersion":"%s","TargetVersion":"%s"}}}]}' "$POLICY_FUNCTION" "$POLICY_FUNCTION" "$cur" "$new")"
  revision="$(python3 -c 'import json,sys; print(json.dumps({"revisionType":"AppSpecContent","appSpecContent":{"content":sys.argv[1]}}))' "$spec")"
  id="$(aws deploy create-deployment --application-name "$CODEDEPLOY_APP" --deployment-group-name "$CODEDEPLOY_GROUP" --revision "$revision" --query deploymentId --output text)"
  echo "CodeDeploy $id: live from $cur to $new, 10 percent now, the rest after 15 minutes"
  aws deploy wait deployment-successful --deployment-id "$id"
fi

echo "resolver: $AGENT_DIGEST"
role="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$AGENT_RUNTIME_ID" --query roleArn --output text)"
aws bedrock-agentcore-control update-agent-runtime \
  --agent-runtime-id "$AGENT_RUNTIME_ID" \
  --role-arn "$role" \
  --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$AGENT_DIGEST\"}}" \
  --query '[agentRuntimeVersion,status]' --output text
