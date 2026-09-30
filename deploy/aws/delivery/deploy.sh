#!/usr/bin/env bash
# The Deploy stage of the delivery pipeline. Runs in CodeBuild after the manual approval, with
# the image digests the Build stage wrote to images.json. Promotes by digest, never by tag:
#   1. the policy Lambda: update the code to the digest (and NW_IMAGE_DIGEST in its environment),
#      publish a version, shift the `live`
#      alias through CodeDeploy (10 percent for 15 minutes, rollback on the alarms);
#   2. the live resolver runtime: update the AgentCore Runtime to the digest (AgentCore keeps
#      the previous version; a session in flight finishes on it). UpdateAgentRuntime resets
#      the optional fields it is not given, so the script reads the runtime's configuration
#      and passes every field back with only the container URI (and NW_IMAGE_DIGEST) changed;
#   3. after each step succeeds, the digest goes to the SSM parameter the stack reads
#      (IMAGE_PARAM_POLICY, IMAGE_PARAM_AGENT), so the next cdk deploy keeps it.
# Before any of it, both digests are verified with Notation against the stack's AWS Signer
# profile (SIGNING_PROFILE_ARN): a digest the Build stage did not sign is never deployed.
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

if [ -n "${SIGNING_PROFILE_ARN:-}" ]; then
  command -v notation >/dev/null || { curl -fsSL -o /tmp/notation.rpm "$NOTATION_RPM" && rpm -U /tmp/notation.rpm; }
  registry="${POLICY_DIGEST%%/*}"
  aws ecr get-login-password | notation login --username AWS --password-stdin "$registry"
  policy_file="$(mktemp)"
  python3 - "$SIGNING_PROFILE_ARN" >"$policy_file" <<'PY'
import json, sys
print(json.dumps({"version": "1.0", "trustPolicies": [{
    "name": "northwind-images",
    "registryScopes": ["*"],
    "signatureVerification": {"level": "strict"},
    "trustStores": ["signingAuthority:aws-signer-ts"],
    "trustedIdentities": [sys.argv[1]],
}]}))
PY
  notation policy import --force "$policy_file"
  for ref in "$POLICY_DIGEST" "$AGENT_DIGEST"; do
    notation verify "$ref" || { echo "signature check failed for $ref: not deploying"; exit 1; }
  done
  rm -f "$policy_file"
fi

echo "policy: $POLICY_DIGEST"
aws lambda update-function-code --function-name "$POLICY_FUNCTION" --image-uri "$POLICY_DIGEST" >/dev/null
aws lambda wait function-updated --function-name "$POLICY_FUNCTION"
# The digest the version serves, as the service reports it on /version (NW_IMAGE_DIGEST): the
# function's environment with that one variable changed, so the published version carries it.
envfile="$(mktemp)"
aws lambda get-function-configuration --function-name "$POLICY_FUNCTION" --query Environment --output json >"$envfile"
python3 - "$envfile" "$POLICY_DIGEST" >"$envfile.new" <<'PY'
import json, sys
env = json.load(open(sys.argv[1])) or {}
variables = dict(env.get("Variables") or {})
variables["NW_IMAGE_DIGEST"] = sys.argv[2].rsplit("@", 1)[-1]
json.dump({"Variables": variables}, sys.stdout)
PY
aws lambda update-function-configuration --function-name "$POLICY_FUNCTION" --environment "file://$envfile.new" >/dev/null
aws lambda wait function-updated --function-name "$POLICY_FUNCTION"
rm -f "$envfile" "$envfile.new"
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

if [ -n "${IMAGE_PARAM_POLICY:-}" ]; then
  aws ssm put-parameter --name "$IMAGE_PARAM_POLICY" --value "$POLICY_DIGEST" --type String --overwrite >/dev/null
  echo "recorded $POLICY_DIGEST in $IMAGE_PARAM_POLICY"
fi

echo "resolver: $AGENT_DIGEST"
current="$(mktemp)"; request="$(mktemp)"
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$AGENT_RUNTIME_ID" --output json >"$current"
python3 - "$current" "$AGENT_DIGEST" "$AGENT_RUNTIME_ID" >"$request" <<'PY'
import json, sys
cur = json.load(open(sys.argv[1]))
keep = (
    "roleArn", "networkConfiguration", "protocolConfiguration", "environmentVariables",
    "description", "authorizerConfiguration", "requestHeaderConfiguration",
    "lifecycleConfiguration", "metadataConfiguration", "filesystemConfigurations",
    "capacityProviderConfiguration", "platformVersion",
)
body = {k: cur[k] for k in keep if cur.get(k) not in (None, {}, [])}
body["agentRuntimeId"] = sys.argv[3]
body["agentRuntimeArtifact"] = {"containerConfiguration": {"containerUri": sys.argv[2]}}
# The digest the runtime serves, as /version reports it.
body["environmentVariables"] = {
    **(body.get("environmentVariables") or {}),
    "NW_IMAGE_DIGEST": sys.argv[2].rsplit("@", 1)[-1],
}
json.dump(body, sys.stdout)
PY
aws bedrock-agentcore-control update-agent-runtime --cli-input-json "file://$request" \
  --query '[agentRuntimeVersion,status]' --output text
for _ in $(seq 1 60); do
  status="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$AGENT_RUNTIME_ID" --query status --output text)"
  case "$status" in
    READY) break ;;
    UPDATE_FAILED|CREATE_FAILED) echo "runtime update failed: $status"; exit 1 ;;
  esac
  sleep 10
done
rm -f "$current" "$request"
if [ -n "${IMAGE_PARAM_AGENT:-}" ]; then
  aws ssm put-parameter --name "$IMAGE_PARAM_AGENT" --value "$AGENT_DIGEST" --type String --overwrite >/dev/null
  echo "recorded $AGENT_DIGEST in $IMAGE_PARAM_AGENT"
fi
