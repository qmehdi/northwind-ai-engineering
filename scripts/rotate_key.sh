#!/usr/bin/env bash
# Rotate the services' x-api-key on the platform and make every service that reads it serve with
# the new value, one owner at a time: the live services (`live`) and each tenant's own services.
#
#   TRACK=aws|gcp|azure scripts/rotate_key.sh                  every owner: live and each tenant
#   TRACK=gcp TENANT=alice scripts/rotate_key.sh               one tenant's key
#   TRACK=aws TENANT=live scripts/rotate_key.sh                the live services' key only
#   TRACK=azure scripts/rotate_key.sh --dry-run                what it would do, nothing changed
#
# (NW_TRACK and NW_TENANT work too. Names follow the platform: NW_ENV on AWS, NW_ENVIRONMENT on
# Google Cloud and Azure; NW_GCP_PROJECT and NW_GCP_RUN_REGION on Google Cloud;
# NW_AZURE_RESOURCE_GROUP on Azure. NW_TENANTS=a,b narrows or replaces the owner discovery.)
#
# What holds each key, and who reads it:
#   AWS    live: the stack's ApiKey secret (output ApiKeySecretArn), read by the policy Lambda,
#          the live resolver and tools runtimes, and copied into the AgentCore API key credential
#          provider `<prefix>-service-api-key`. Tenant: `<prefix>-<tenant>-api-key` (output
#          TenantApiKeys), read by the tenant's resolver runtime `<prefix>_<tenant>_resolver`.
#   GCP    live: `<environment>-api-key`, read by the live Cloud Run services. Tenant:
#          `<environment>-<tenant>-api-key`, read by the tenant's triage, semantic, policy and
#          MCP services. Agent Engine reads it at start and calls its tools through MCP with an
#          ID token, so an engine keeps working on the old value until it next starts.
#   Azure  every owner: Key Vault `<environment>-<owner>-api-key`, the map {"<owner>": "<key>"}
#          the owner's policy, agent and MCP apps read by reference.
#
# The safety properties, per owner:
#   - No traffic split during the rotation. A canary would refuse the new key on one slice and the
#     old key on the other, and there is no key rotation that is right for a slice of traffic. The
#     script refuses to start while a Lambda alias carries a weighted version, a Cloud Run service
#     splits traffic between revisions or a live Container App splits between revisions, and it
#     moves every service to its new revision or version at once (the Lambda alias directly,
#     not through CodeDeploy).
#   - Old versions are disabled only after every service of the owner serves again: AWSPREVIOUS
#     is removed from the old secret version, older Secret Manager versions and older Key Vault
#     versions are disabled.
#   - The new key is printed once at the end and stored nowhere else. It never appears on a
#     command line (a file with mode 600, or standard input).
# Safe to rerun: every step is complete on its own, and a rerun rotates once more.
set -euo pipefail
cd "$(dirname "$0")/.."

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run|-n) DRY_RUN=1 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument $arg; usage: TRACK=aws|gcp|azure [TENANT=<handle>|live|all] $0 [--dry-run]" >&2; exit 2 ;;
  esac
done
TRACK="${TRACK:-${NW_TRACK:-}}"
TARGET="${TENANT:-${NW_TENANT_ROTATE:-all}}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT
ROTATED=()

say() { echo "$*"; }
# Every call that changes something goes through run: printed and skipped with --dry-run.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "would run: $*"
  else
    "$@"
  fi
}
new_key() { # 40 hexadecimal characters, the shape the deploys generate
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 20; else python3 -c 'import secrets; print(secrets.token_hex(20))'; fi
}
check_handle() {
  [[ "$1" =~ ^[a-z][a-z0-9]{1,15}$ ]] || { echo "not an owner name: $1" >&2; exit 2; }
}
# The owners to rotate: `all` is live plus every tenant the platform knows, else the one named.
select_owners() { # known tenants...
  if [ "$TARGET" = all ]; then
    # live first, then each tenant once
    printf '%s\n' live "$@" | awk 'NF && !seen[$0]++' | tr '\n' ' '
    return
  fi
  check_handle "$TARGET"
  if [ "$TARGET" != live ]; then
    local t found=0
    for t in "$@"; do [ "$t" = "$TARGET" ] && found=1; done
    [ "$found" = 1 ] || { echo "no tenant $TARGET on this platform (known: ${*:-none})" >&2; exit 2; }
  fi
  echo "$TARGET"
}
tenants_from_env() { # NW_TENANTS=a,b as words, or nothing
  [ -n "${NW_TENANTS:-}" ] && echo "${NW_TENANTS//,/ }"
  return 0
}

# ----- AWS -------------------------------------------------------------------------------------

aws_output() { # key: from deploy/aws/outputs.json, the stack `<prefix>-platform`
  python3 - "$1" "$PREFIX-platform" <<'PY'
import json, sys
try:
    data = json.load(open("deploy/aws/outputs.json"))
except (OSError, ValueError):
    sys.exit("no deploy/aws/outputs.json: run make deploy-aws, or set NW_API_KEY_SECRET_ARN")
stack = data.get(sys.argv[2]) or {}
print(stack.get(sys.argv[1], ""))
PY
}

aws_tenant_secret() { # tenant
  python3 -c 'import sys; print(dict(p.split("=", 1) for p in sys.argv[1].split(",") if "=" in p).get(sys.argv[2], ""))' "$TENANT_KEYS" "$1"
}

aws_runtime_id() { # runtime name, or nothing when it does not exist
  aws bedrock-agentcore-control list-agent-runtimes --output json \
    | python3 -c 'import json,sys; print(next((r["agentRuntimeId"] for r in json.load(sys.stdin).get("agentRuntimes", []) if r["agentRuntimeName"] == sys.argv[1]), ""))' "$1"
}

aws_guard_alias() { # function: refuse while CodeDeploy (or anyone) splits the live alias
  local weights
  weights="$(aws lambda get-alias --function-name "$1" --name live --query 'RoutingConfig.AdditionalVersionWeights' --output json 2>/dev/null || echo null)"
  if [ "$weights" != null ] && [ "$weights" != "{}" ]; then
    echo "$1: the live alias splits traffic ($weights); finish or stop the deployment first" >&2
    exit 1
  fi
}

aws_roll_function() { # function: new version with NW_KEY_ROTATED_AT, alias moved at once
  local fn="$1" v
  run aws lambda wait function-updated --function-name "$fn"
  aws lambda get-function-configuration --function-name "$fn" --query Environment --output json >"$WORK/env.json"
  python3 - "$WORK/env.json" "$STAMP" >"$WORK/env.new.json" <<'PY'
import json, sys
env = json.load(open(sys.argv[1])) or {}
variables = dict(env.get("Variables") or {})
variables["NW_KEY_ROTATED_AT"] = sys.argv[2]
json.dump({"Variables": variables}, sys.stdout)
PY
  run aws lambda update-function-configuration --function-name "$fn" --environment "file://$WORK/env.new.json" >/dev/null
  run aws lambda wait function-updated --function-name "$fn"
  if [ "$DRY_RUN" = 1 ]; then
    say "would publish a version of $fn and move its live alias to it"
    return
  fi
  v="$(aws lambda publish-version --function-name "$fn" --query Version --output text)"
  aws lambda update-alias --function-name "$fn" --name live --function-version "$v" --routing-config '{}' >/dev/null
  say "$fn: live -> version $v (reads the new key at its next cold start)"
}

aws_roll_runtime() { # runtime name: a new runtime version with NW_KEY_ROTATED_AT, then READY
  local name="$1" id status
  id="$(aws_runtime_id "$name")"
  if [ -z "$id" ]; then
    say "$name: not deployed, nothing to restart"
    return
  fi
  aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$id" --output json >"$WORK/runtime.json"
  # UpdateAgentRuntime resets the optional fields it is not given: pass every one back.
  python3 - "$WORK/runtime.json" "$id" "$STAMP" >"$WORK/runtime.new.json" <<'PY'
import json, sys
cur = json.load(open(sys.argv[1]))
keep = (
    "agentRuntimeArtifact", "roleArn", "networkConfiguration", "protocolConfiguration",
    "environmentVariables", "description", "authorizerConfiguration",
    "requestHeaderConfiguration", "lifecycleConfiguration", "metadataConfiguration",
    "filesystemConfigurations", "capacityProviderConfiguration", "platformVersion",
)
body = {k: cur[k] for k in keep if cur.get(k) not in (None, {}, [])}
body["agentRuntimeId"] = sys.argv[2]
body["environmentVariables"] = {**(body.get("environmentVariables") or {}), "NW_KEY_ROTATED_AT": sys.argv[3]}
json.dump(body, sys.stdout)
PY
  run aws bedrock-agentcore-control update-agent-runtime --cli-input-json "file://$WORK/runtime.new.json" >/dev/null
  if [ "$DRY_RUN" = 1 ]; then return; fi
  for _ in $(seq 1 60); do
    status="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$id" --query status --output text)"
    case "$status" in
      READY) say "$name: new runtime version READY (reads the new key at start)"; return ;;
      UPDATE_FAILED|CREATE_FAILED) echo "$name: update failed ($status); the old key version stays enabled" >&2; exit 1 ;;
    esac
    sleep 10
  done
  echo "$name: not READY after 10 minutes; the old key version stays enabled" >&2
  exit 1
}

aws_rotate() { # owner
  local owner="$1" arn old key runtimes=() fns=()
  if [ "$owner" = live ]; then
    arn="${NW_API_KEY_SECRET_ARN:-$(aws_output ApiKeySecretArn)}"
    fns=("$PREFIX-policy")
    runtimes=("${UNDER}_live_resolver" "${UNDER}_tools")
  else
    arn="$(aws_tenant_secret "$owner")"
    runtimes=("${UNDER}_${owner}_resolver")
  fi
  [ -n "$arn" ] || { echo "$owner: no API key secret in the stack outputs" >&2; exit 2; }
  local fn
  for fn in ${fns[@]+"${fns[@]}"}; do aws_guard_alias "$fn"; done
  old="$(aws secretsmanager describe-secret --secret-id "$arn" --query 'VersionIdsToStages' --output json \
    | python3 -c 'import json,sys; print(next((v for v, s in json.load(sys.stdin).items() if "AWSCURRENT" in s), ""))')"
  say "$owner: secret $arn (current version ${old:-none})"
  key="$(new_key)"
  printf '{"SecretId": "%s", "SecretString": "%s"}' "$arn" "$key" >"$WORK/secret.json"
  chmod 600 "$WORK/secret.json"
  run aws secretsmanager put-secret-value --cli-input-json "file://$WORK/secret.json" >/dev/null
  if [ "$owner" = live ]; then
    # The gateway's credential provider holds a copy of the value (the stack resolved it at deploy).
    printf '{"name": "%s-service-api-key", "apiKey": "%s"}' "$PREFIX" "$key" >"$WORK/provider.json"
    chmod 600 "$WORK/provider.json"
    run aws bedrock-agentcore-control update-api-key-credential-provider --cli-input-json "file://$WORK/provider.json" >/dev/null
  fi
  for fn in ${fns[@]+"${fns[@]}"}; do aws_roll_function "$fn"; done
  local r
  for r in "${runtimes[@]}"; do aws_roll_runtime "$r"; done
  # Every reader serves with the new value: the old version loses its label.
  if [ -n "$old" ]; then
    run aws secretsmanager update-secret-version-stage --secret-id "$arn" --version-stage AWSPREVIOUS --remove-from-version-id "$old" >/dev/null
    say "$owner: old version $old no longer labelled"
  fi
  [ "$DRY_RUN" = 1 ] || ROTATED+=("$owner $key")
}

# ----- Google Cloud ----------------------------------------------------------------------------

gcp_services() { # owner: the Cloud Run services that read the owner's key
  if [ "$1" = live ]; then
    echo "$ENVIRONMENT-live-triage $ENVIRONMENT-live-semantic $ENVIRONMENT-live-policy $ENVIRONMENT-live-agent"
  else
    echo "$ENVIRONMENT-$1-triage $ENVIRONMENT-$1-semantic $ENVIRONMENT-$1-policy $ENVIRONMENT-$1-mcp"
  fi
}

gcp_guard_split() { # service: refuse while traffic is split between revisions
  local served
  served="$(gcloud run services describe "$1" --project "$PROJECT" --region "$REGION" --format=json \
    | python3 -c 'import json,sys; print(sum(1 for t in json.load(sys.stdin).get("status", {}).get("traffic", []) if t.get("percent", 0) > 0))')"
  if [ "$served" -gt 1 ]; then
    echo "$1: traffic is split between $served revisions (a canary?); promote or roll back first" >&2
    exit 1
  fi
}

gcp_rotate() { # owner
  local owner="$1" secret newest key svc present=()
  if [ "$owner" = live ]; then secret="$ENVIRONMENT-api-key"; else secret="$ENVIRONMENT-$owner-api-key"; fi
  for svc in $(gcp_services "$owner"); do
    if gcloud run services describe "$svc" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
      gcp_guard_split "$svc"
      present+=("$svc")
    fi
  done
  say "$owner: secret $secret, services: ${present[*]:-none deployed}"
  key="$(new_key)"
  if [ "$DRY_RUN" = 1 ]; then
    say "would run: gcloud secrets versions add $secret --project $PROJECT --data-file=- (the key on standard input)"
  else
    printf %s "$key" | gcloud secrets versions add "$secret" --project "$PROJECT" --data-file=- >/dev/null
  fi
  for svc in ${present[@]+"${present[@]}"}; do
    # A new revision resolves `latest`; it takes all the traffic at once, never a slice.
    run gcloud run services update "$svc" --project "$PROJECT" --region "$REGION" \
      --update-env-vars "NW_KEY_ROTATED_AT=$STAMP" --quiet >/dev/null
    run gcloud run services update-traffic "$svc" --project "$PROJECT" --region "$REGION" --to-latest --quiet >/dev/null
    say "$svc: new revision serves 100 percent with the new key"
  done
  # Every service serves again: disable every older enabled version.
  newest="$(gcloud secrets versions list "$secret" --project "$PROJECT" --filter="state:enabled" --sort-by="~createTime" --limit=1 --format="value(name)")"
  local v
  for v in $(gcloud secrets versions list "$secret" --project "$PROJECT" --filter="state:enabled" --format="value(name)"); do
    if [ "$DRY_RUN" = 0 ] && [ "$v" = "$newest" ]; then continue; fi
    run gcloud secrets versions disable "$v" --secret "$secret" --project "$PROJECT" --quiet >/dev/null
    [ "$DRY_RUN" = 1 ] || say "$secret: version $v disabled"
  done
  if [ "$owner" != live ]; then
    say "$owner: the Agent Engine resolver reads the key at start and reaches its tools through MCP with an ID token; it picks the new value up at its next start"
  fi
  [ "$DRY_RUN" = 1 ] || ROTATED+=("$owner $key")
}

# ----- Azure -----------------------------------------------------------------------------------

azure_out() { python3 deploy/azure/scripts/azure_params.py get "$1"; }

azure_guard_split() { # app: refuse while a multiple-revision app splits traffic
  local served
  served="$(az containerapp ingress traffic show -n "$1" -g "$RG" -o json \
    | python3 -c 'import json,sys; print(sum(1 for r in json.load(sys.stdin) if r.get("weight", 0) > 0))')"
  if [ "$served" -gt 1 ]; then
    echo "$1: traffic is split between $served revisions (a canary?); make approve-azure or roll back first" >&2
    exit 1
  fi
}

azure_rotate() { # owner
  local owner="$1" secret="$ENVIRONMENT-$1-api-key" key app kind present=() versions newest
  for kind in policy agent mcp; do
    app="$ENVIRONMENT-$owner-$kind"
    if az containerapp show -n "$app" -g "$RG" -o none 2>/dev/null; then
      azure_guard_split "$app"
      present+=("$app")
    fi
  done
  say "$owner: Key Vault $VAULT secret $secret, apps: ${present[*]:-none deployed}"
  key="$(new_key)"
  # The apps accept a key map: the key id is the owner (nw/auth.py).
  python3 -c 'import json,sys; print(json.dumps({sys.argv[1]: sys.argv[2]}), end="")' "$owner" "$key" >"$WORK/secret.txt"
  chmod 600 "$WORK/secret.txt"
  run az keyvault secret set --vault-name "$VAULT" -n "$secret" --file "$WORK/secret.txt" \
    --content-type "x-api-key map for the apps of $ENVIRONMENT-$owner" -o none
  for app in ${present[@]+"${present[@]}"}; do
    # A new revision reads the Key Vault reference again; a multiple-revision app moves all of
    # its traffic to it at once.
    run az containerapp update -n "$app" -g "$RG" --set-env-vars "NW_KEY_ROTATED_AT=$STAMP" \
      --revision-suffix "k$(date +%m%d%H%M%S)" -o none
    if [ "$(az containerapp show -n "$app" -g "$RG" --query properties.configuration.activeRevisionsMode -o tsv)" = Multiple ]; then
      run az containerapp ingress traffic set -n "$app" -g "$RG" --revision-weight latest=100 -o none
    fi
    if [ "$DRY_RUN" = 0 ]; then
      local state=""
      for _ in $(seq 1 60); do
        state="$(az containerapp revision show -n "$app" -g "$RG" --revision "$(az containerapp show -n "$app" -g "$RG" --query properties.latestRevisionName -o tsv)" --query properties.healthState -o tsv 2>/dev/null || true)"
        [ "$state" = Healthy ] && break
        sleep 10
      done
      [ "$state" = Healthy ] || { echo "$app: new revision not healthy after 10 minutes; the old key version stays enabled" >&2; exit 1; }
    fi
    say "$app: new revision serves 100 percent with the new key"
  done
  # Every app serves again: disable every older version of the secret.
  versions="$(az keyvault secret list-versions --vault-name "$VAULT" -n "$secret" -o json)"
  newest="$(python3 -c 'import json,sys; v=[x for x in json.loads(sys.argv[1]) if x.get("attributes", {}).get("enabled")]; v.sort(key=lambda x: x["attributes"]["created"]); print(v[-1]["id"] if v else "")' "$versions")"
  local id
  for id in $(python3 -c 'import json,sys; print(" ".join(x["id"] for x in json.loads(sys.argv[1]) if x.get("attributes", {}).get("enabled")))' "$versions"); do
    if [ "$DRY_RUN" = 0 ] && [ "$id" = "$newest" ]; then continue; fi
    run az keyvault secret set-attributes --id "$id" --enabled false -o none
    [ "$DRY_RUN" = 1 ] || say "$secret: version ${id##*/} disabled"
  done
  [ "$DRY_RUN" = 1 ] || ROTATED+=("$owner $key")
}

# ----- main ------------------------------------------------------------------------------------

[ "$DRY_RUN" = 1 ] && say "dry run: nothing is changed; with --dry-run every old version is listed as one the run would disable"
case "$TRACK" in
  aws)
    PREFIX="northwind${NW_ENV:+-$NW_ENV}"
    UNDER="${PREFIX//-/_}"
    TENANT_KEYS="${NW_AWS_TENANT_API_KEYS:-$(aws_output TenantApiKeys)}"
    # shellcheck disable=SC2046 # the tenant list is words by design
    OWNERS="$(select_owners $(tenants_from_env) $(python3 -c 'import sys; print(" ".join(p.split("=", 1)[0] for p in sys.argv[1].split(",") if "=" in p))' "$TENANT_KEYS"))"
    for o in $OWNERS; do aws_rotate "$o"; done ;;
  gcp)
    PROJECT="${NW_GCP_PROJECT:?set NW_GCP_PROJECT}"
    REGION="${NW_GCP_RUN_REGION:-us-central1}"
    ENVIRONMENT="${NW_ENVIRONMENT:-northwind}"
    KNOWN="$(tenants_from_env)"
    if [ -z "$KNOWN" ] && [ "$TARGET" != live ]; then
      KNOWN="$(gcloud secrets list --project "$PROJECT" --filter="name~^projects/.*/secrets/$ENVIRONMENT-[a-z0-9]+-api-key$" --format="value(name)" \
        | sed -e "s|.*/||" -e "s|^$ENVIRONMENT-||" -e 's|-api-key$||' | grep -v '^live$' | tr '\n' ' ' || true)"
    fi
    # shellcheck disable=SC2086 # the tenant list is words by design
    OWNERS="$(select_owners $KNOWN)"
    for o in $OWNERS; do gcp_rotate "$o"; done ;;
  azure)
    ENVIRONMENT="${NW_ENVIRONMENT:-northwind}"
    RG="${NW_AZURE_RESOURCE_GROUP:-rg-$ENVIRONMENT}"
    VAULT="${NW_AZURE_KEY_VAULT:-$(azure_out NW_AZURE_KEY_VAULT)}"
    [ -n "$VAULT" ] || { echo "no NW_AZURE_KEY_VAULT in deploy/azure/outputs.json: run make deploy-azure" >&2; exit 2; }
    KNOWN="$(tenants_from_env)"
    if [ -z "$KNOWN" ] && [ "$TARGET" != live ]; then
      KNOWN="$(azure_out NW_AZURE_OWNERS | python3 -c 'import json,sys; print(" ".join(o for o in json.load(sys.stdin) if o != "live"))')"
    fi
    # shellcheck disable=SC2086 # the tenant list is words by design
    OWNERS="$(select_owners $KNOWN)"
    for o in $OWNERS; do azure_rotate "$o"; done ;;
  *) echo "usage: TRACK=aws|gcp|azure [TENANT=<handle>|live|all] scripts/rotate_key.sh [--dry-run]" >&2; exit 2 ;;
esac

if [ "$DRY_RUN" = 1 ]; then
  say "dry run done: owners $OWNERS"
  exit 0
fi
say "new API keys (printed once, stored nowhere else; hand each tenant its own):"
for line in "${ROTATED[@]}"; do say "  $line"; done
