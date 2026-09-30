#!/usr/bin/env bash
# The Azure platform: one environment in one resource group (deploy/azure, Bicep).
#
#   scripts/deploy_azure.sh setup|build|what-if|deploy|outputs|tenants|status|stop|start|destroy|release|approve|keys|indexes
#
# NW_MODE=cohort|solo (default cohort), NW_TENANTS=alice,bob (cohort), NW_ENVIRONMENT (default
# northwind), NW_AZURE_RESOURCE_GROUP (default rg-<environment>), NW_AZURE_LOCATION (default
# eastus2), NW_IMAGE_TAG (default the git SHA, `-dirty` with changes; the apps use it once
# `make images-azure` pushed it), NW_ALERT_EMAIL, NW_BUDGET_USD, NW_GATEWAY_KIND=apim|litellm,
# NW_APIM_SKU, NW_SEARCH_SKU, NW_TENANT_USERS=alice=<Entra object id>,..., NW_GITHUB_REPOSITORY,
# NW_ADO_ISSUER, NW_ADO_SUBJECT, NW_RETRAIN_ENABLED, NW_DEFENDER, NW_PURVIEW,
# NW_TENANT_BUDGET_USD (LiteLLM keys), NW_TENANT_TOKENS_PER_MONTH (APIM quota), NW_EGRESS_CONTROL
# (default true), NW_AZURE_EU (default true: the EU Foundry resource), NW_AZURE_EU_LOCATION
# (default swedencentral), NW_ADO_BUILDER_SUBJECT, NW_CANARY_PERCENT (default 10), REASON (approve).
#
# `what-if` and everything after `build` call Azure; `build` does not.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
AZ_DIR="$ROOT/deploy/azure"
ACTION="${1:-build}"
export NW_ENVIRONMENT="${NW_ENVIRONMENT:-northwind}"
export NW_AZURE_LOCATION="${NW_AZURE_LOCATION:-eastus2}"
export NW_MODE="${NW_MODE:-cohort}"
ENVIRONMENT="$NW_ENVIRONMENT"
RG="${NW_AZURE_RESOURCE_GROUP:-rg-$ENVIRONMENT}"
LOCATION="$NW_AZURE_LOCATION"
OUTPUTS="$AZ_DIR/outputs.json"
PARAMS_PY=(python3 "$AZ_DIR/scripts/azure_params.py")
CANARY="${NW_CANARY_PERCENT:-10}"

image_tag() {
  if [ -n "${NW_IMAGE_TAG:-}" ]; then echo "$NW_IMAGE_TAG"; return; fi
  local sha
  if sha="$(git rev-parse --short HEAD 2>/dev/null)"; then
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then echo "$sha-dirty"; else echo "$sha"; fi
  else
    echo latest
  fi
}
TAG="$(image_tag)"

# The Bicep CLI: `az bicep` when the Azure CLI is there, else the standalone binary that
# `make setup-azure` installs into ~/.azure/bin (the same place `az bicep install` uses).
bicep_build() { # file outfile
  if command -v az >/dev/null 2>&1; then az bicep build --file "$1" --outfile "$2"
  elif [ -x "$HOME/.azure/bin/bicep" ]; then "$HOME/.azure/bin/bicep" build "$1" --outfile "$2"
  else bicep build "$1" --outfile "$2"; fi
}
bicep_lint() {
  if command -v az >/dev/null 2>&1; then az bicep lint --file "$1"
  elif [ -x "$HOME/.azure/bin/bicep" ]; then "$HOME/.azure/bin/bicep" lint "$1"
  else bicep lint "$1"; fi
}
out() { "${PARAMS_PY[@]}" get "$1"; }
owners() { "${PARAMS_PY[@]}" owners; }
have_outputs() { [ -f "$OUTPUTS" ]; }
group_exists() { [ "$(az group exists -n "$RG")" = "true" ]; }
random_key() { python3 -c 'import secrets; print(secrets.token_hex(20))'; }

# Alerts Management at resource group scope (GET {scope}/providers/Microsoft.AlertsManagement/
# alerts, api-version 2019-03-01, where {scope} may be a subscription, a resource group or a
# resource): the deployer's custom role grants alerts/read on this group only, so a query at
# subscription scope would fail or come back empty. `alerts_url` is the first page of alerts
# still firing in the last 30 days; `fired_live_alerts` follows nextLink and prints the rules of
# the live apps' alerts, one per line, and returns 1 when any page cannot be read or parsed.
alerts_url() {
  local sub; sub="$(out NW_AZURE_SUBSCRIPTION_ID)" && [ -n "$sub" ] || return 1
  echo "https://management.azure.com/subscriptions/$sub/resourceGroups/$RG/providers/Microsoft.AlertsManagement/alerts?api-version=2019-03-01&monitorCondition=Fired&timeRange=30d&pageCount=250"
}
fired_live_alerts() {
  local url page parsed
  url="$(alerts_url)" || return 1
  while [ -n "$url" ]; do
    page="$(az rest --method get --url "$url" -o json)" || return 1
    parsed="$(printf '%s' "$page" | python3 -c '
import json,sys
d=json.load(sys.stdin)
if not isinstance(d,dict) or not isinstance(d.get("value"),list): sys.exit(1)
print(d.get("nextLink") or "")
for a in d["value"]:
    rule=str(((a.get("properties") or {}).get("essentials") or {}).get("alertRule") or "")
    if sys.argv[1]+"-live" in rule: print(rule)' "$ENVIRONMENT")" || return 1
    url="$(printf '%s\n' "$parsed" | head -n 1)"
    printf '%s\n' "$parsed" | tail -n +2
  done
}

# A secret kept across deploys: the Key Vault value when the platform exists, a new one otherwise.
kept_secret() { # secret-name
  local vault value=""
  if have_outputs && vault="$(out NW_AZURE_KEY_VAULT)" && [ -n "$vault" ]; then
    value="$(az keyvault secret show --vault-name "$vault" -n "$1" --query value -o tsv 2>/dev/null || true)"
  fi
  if [ -n "$value" ]; then echo "$value"; else random_key; fi
}

# What the promotion drills changed and a redeploy must not undo: the traffic of every online
# endpoint, and the image and traffic of the live apps (Terraform's ignore_changes, by hand).
capture_state() {
  export NW_ENDPOINT_TRAFFIC="{}" NW_LIVE_APPS="{}"
  have_outputs && group_exists || return 0
  local ws; ws="$(out NW_AZURE_ML_WORKSPACE)"
  NW_ENDPOINT_TRAFFIC="$(az ml online-endpoint list -g "$RG" -w "$ws" -o json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.dumps({e["name"]: e.get("traffic") or {} for e in json.load(sys.stdin) if e.get("traffic")}))' || echo '{}')"
  local apps="{}"
  for kind in policy agent; do
    local name="$ENVIRONMENT-live-$kind" doc
    doc="$(az containerapp show -n "$name" -g "$RG" -o json 2>/dev/null || true)"
    [ -n "$doc" ] || continue
    apps="$(python3 -c '
import json,sys
apps=json.loads(sys.argv[1]); doc=json.loads(sys.argv[2])
props=doc["properties"]
apps[sys.argv[3]]={"image": props["template"]["containers"][0]["image"],
                  "traffic": props["configuration"]["ingress"].get("traffic") or [{"latestRevision": True, "weight": 100}]}
print(json.dumps(apps))' "$apps" "$doc" "$name")"
  done
  NW_LIVE_APPS="$apps"
}

# The images run by the apps: the tag when `make images-azure` pushed it, a placeholder before.
deploy_image_tag() {
  export NW_DEPLOY_IMAGE_TAG=""
  have_outputs && group_exists || return 0
  local acr; acr="$(out NW_AZURE_ACR)"
  if az acr repository show-tags -n "$acr" --repository nw-policy -o tsv 2>/dev/null | grep -qx "$TAG"; then
    NW_DEPLOY_IMAGE_TAG="$TAG"
  else
    echo "no nw-policy:$TAG in $acr yet: the apps run a placeholder until make images-azure and a redeploy"
  fi
}

write_params() { # file
  "${PARAMS_PY[@]}" check-tenants
  local sub; sub="$(az account show --query id -o tsv)"
  export NW_ENDPOINT_SCOPE; NW_ENDPOINT_SCOPE="$("${PARAMS_PY[@]}" scope "$sub" "$RG" "$ENVIRONMENT")"
  export NW_API_KEY_VALUE; NW_API_KEY_VALUE="$(kept_secret "$ENVIRONMENT-api-key")"
  export NW_ADMIN_OBJECT_ID="${NW_ADMIN_OBJECT_ID:-$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)}"
  # One x-api-key per owner, kept across deploys: the Key Vault secret holds the owner's key
  # map ({"<owner>": "<key>"}), which is what the apps read.
  local api_keys="{}"
  for o in $(owners); do
    api_keys="$(python3 -c '
import json,sys
d=json.loads(sys.argv[1]); owner=sys.argv[2]; kept=sys.argv[3]
try: value=json.loads(kept).get(owner) or kept
except ValueError: value=kept
d[owner]=value; print(json.dumps(d))' "$api_keys" "$o" "$(kept_secret "$ENVIRONMENT-$o-api-key")")"
  done
  export NW_API_KEYS="$api_keys"
  if [ "${NW_GATEWAY_KIND:-apim}" = "litellm" ]; then
    export NW_LITELLM_MASTER_KEY NW_POSTGRES_PASSWORD NW_GATEWAY_KEYS
    NW_LITELLM_MASTER_KEY="sk-$(kept_secret "$ENVIRONMENT-gateway-master-key" | sed 's/^sk-//')"
    export NW_LITELLM_SALT_KEY; NW_LITELLM_SALT_KEY="$(kept_secret "$ENVIRONMENT-gateway-salt-key")"
    NW_POSTGRES_PASSWORD="${NW_POSTGRES_PASSWORD:-$(random_key)Aa1}"
    local keys="{}"
    for o in $(owners); do
      keys="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); d[sys.argv[2]]=sys.argv[3]; print(json.dumps(d))' \
        "$keys" "$o" "sk-$(kept_secret "$ENVIRONMENT-$o-gateway-key" | sed 's/^sk-//')")"
    done
    NW_GATEWAY_KEYS="$keys"
  fi
  capture_state
  deploy_image_tag
  "${PARAMS_PY[@]}" params "$1"
}

# Blob uploads need the Storage Blob Data Contributor role the deployment just granted; role
# assignments take a minute or two to reach the data plane, hence the retries.
retry() { local n=0; until "$@"; do n=$((n+1)); [ "$n" -ge 8 ] && return 1; echo "  waiting for the role to propagate ($n)"; sleep 20; done; }

upload_data() {
  local lake ws container
  lake="$(out NW_AZURE_STORAGE_ACCOUNT)"; ws="$(out NW_AZURE_ML_WORKSPACE)"
  echo "== data: tickets and the two production summaries"
  retry az storage blob upload --auth-mode login --account-name "$lake" -c data -n tickets/tickets.jsonl -f data/tickets.jsonl --overwrite --only-show-errors >/dev/null
  for kind in triage semantic; do
    retry az storage blob upload --auth-mode login --account-name "$lake" -c baselines -n "${kind}_production.json" \
      -f "data/golden/${kind}_production.json" --overwrite --only-show-errors >/dev/null
  done
  # nw/platform/azure.py defaults a pipeline to the workspace's default datastore: the same files
  # at tickets/ and baselines/ there.
  container="$(az ml datastore show -n workspaceblobstore -g "$RG" -w "$ws" --query container_name -o tsv)"
  local store; store="$(az ml datastore show -n workspaceblobstore -g "$RG" -w "$ws" --query account_name -o tsv)"
  retry az storage blob upload --auth-mode login --account-name "$store" -c "$container" -n tickets/tickets.jsonl -f data/tickets.jsonl --overwrite --only-show-errors >/dev/null
  for kind in triage semantic; do
    retry az storage blob upload --auth-mode login --account-name "$store" -c "$container" -n "baselines/${kind}_production.json" \
      -f "data/golden/${kind}_production.json" --overwrite --only-show-errors >/dev/null
  done
  echo "  lake $lake (data, baselines) and workspaceblobstore ($store/$container)"
}

# Azure AI Search indexes are data plane: one per owner from search/policies-index.json (the
# definition nw/platform/azure.py would create), and the owner's identity gets the two search
# roles on that index alone. When the service refuses an index-scoped assignment the script
# stops: granting the role on the service instead would let every tenant read and write every
# index, which is exactly what the index scope is for (fails closed; nothing is widened).
create_indexes() {
  local search search_id foundry embedding dims sub body
  search="$(out NW_AZURE_SEARCH_ENDPOINT)"; foundry="$(out NW_AZURE_FOUNDRY_ENDPOINT)"
  embedding="$(out NW_AZURE_EMBEDDING_DEPLOYMENT)"; dims="$(out NW_AZURE_EMBEDDING_DIMENSIONS)"
  sub="$(out NW_AZURE_SUBSCRIPTION_ID)"
  search_id="/subscriptions/$sub/resourceGroups/$RG/providers/Microsoft.Search/searchServices/$(out NW_AZURE_SEARCH_SERVICE)"
  body="$(mktemp)"
  echo "== search indexes"
  for o in $(owners); do
    local index="$ENVIRONMENT-$o-policies" principal
    sed -e "s|__NAME__|$index|" -e "s|__FOUNDRY__|$foundry|" -e "s|__EMBEDDING__|$embedding|g" -e "s|__DIMENSIONS__|$dims|" \
      "$AZ_DIR/search/policies-index.json" > "$body"
    retry az rest --method put --url "$search/indexes/$index?api-version=2024-07-01" --resource https://search.azure.com \
      --headers "Content-Type=application/json" --body "@$body" -o none
    principal="$(az identity show -n "$ENVIRONMENT-$o-id" -g "$RG" --query principalId -o tsv)"
    for role in "Search Index Data Contributor" "Search Service Contributor"; do
      if ! az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal \
           --role "$role" --scope "$search_id/indexes/$index" -o none; then
        rm -f "$body"
        echo "  $role on $index was refused at index scope; stopping rather than granting it on the whole service" >&2
        echo "  (every tenant would then read every index). Check the role and rerun: scripts/deploy_azure.sh indexes" >&2
        return 1
      fi
    done
    echo "  $index"
  done
  rm -f "$body"
}

# Every role assignment of one principal, at every scope (the resource group, and the child
# scopes the template and this script use: blob containers, secrets, endpoints, the registry,
# the search index, the Foundry project). `az role assignment delete -g` alone leaves the
# child-scope ones behind, orphaned once the identity is gone.
remove_assignments() { # principal-object-id
  [ -n "$1" ] || return 0
  local ids
  ids="$(az role assignment list --assignee "$1" --all --query "[].id" -o tsv 2>/dev/null || true)"
  [ -z "$ids" ] && return 0
  # shellcheck disable=SC2086
  az role assignment delete --ids $ids -o none && echo "  removed $(printf '%s\n' "$ids" | wc -l | tr -d ' ') role assignments of $1"
}

# LiteLLM mode: register every owner's key (kept in Key Vault) with the proxy, with a budget.
register_keys() {
  if [ "$(out NW_AZURE_GATEWAY_KIND)" != "litellm" ]; then
    echo "API Management gateway: the subscription keys are the tenant keys (Key Vault: $ENVIRONMENT-<owner>-gateway-key)"
    return 0
  fi
  local url vault master
  url="$(out NW_GATEWAY_URL)"; vault="$(out NW_AZURE_KEY_VAULT)"
  master="$(az keyvault secret show --vault-name "$vault" -n "$ENVIRONMENT-gateway-master-key" --query value -o tsv)"
  for o in $(owners); do
    local key; key="$(az keyvault secret show --vault-name "$vault" -n "$ENVIRONMENT-$o-gateway-key" --query value -o tsv)"
    local body; body="$(python3 -c 'import json,sys; print(json.dumps({"key": sys.argv[1], "key_alias": sys.argv[2], "max_budget": float(sys.argv[3]), "budget_duration": "30d", "metadata": {"tenant": sys.argv[4]}}))' \
      "$key" "$ENVIRONMENT-$o" "${NW_TENANT_BUDGET_USD:-10}" "$o")"
    if curl -fsS -X POST "$url/key/generate" -H "Authorization: Bearer $master" -H "Content-Type: application/json" -d "$body" >/dev/null 2>&1 \
       || curl -fsS -X POST "$url/key/update" -H "Authorization: Bearer $master" -H "Content-Type: application/json" -d "$body" >/dev/null; then
      echo "  key for $o registered"
    fi
  done
}

live_apps() { echo "$ENVIRONMENT-live-policy $ENVIRONMENT-live-agent"; }

# A release runs only signed images: cosign verifies the digest against the platform's Key Vault
# key (scripts/images_azure.sh signs). A missing cosign, a missing or a bad signature stops the
# release; NW_ALLOW_UNSIGNED=1 overrides, and says so.
verify_image() { # registry/repo@digest
  if [ "${NW_ALLOW_UNSIGNED:-0}" = "1" ]; then echo "  NW_ALLOW_UNSIGNED=1: not verifying $1" >&2; return 0; fi
  command -v cosign >/dev/null 2>&1 || { echo "cosign is not installed; cannot verify $1, not releasing" >&2; return 1; }
  AZURE_AUTH_METHOD=cli cosign verify --insecure-ignore-tlog=true \
    --key "azurekms://$(out NW_AZURE_KEY_VAULT).vault.azure.net/$(out NW_AZURE_SIGNING_KEY)" "$1" >/dev/null \
    || { echo "signature check failed for $1, not releasing" >&2; return 1; }
  echo "  verified signature of $1"
}

case "$ACTION" in
  setup)
    # The Bicep CLI: through the Azure CLI when it is installed, otherwise the standalone release
    # binary from github.com/Azure/bicep into ~/.azure/bin, where `az bicep install` puts it. No
    # administrator rights either way.
    if command -v az >/dev/null 2>&1; then az bicep install && az bicep version; exit 0; fi
    case "$(uname -s)-$(uname -m)" in
      Darwin-arm64) ASSET=bicep-osx-arm64 ;; Darwin-x86_64) ASSET=bicep-osx-x64 ;;
      Linux-aarch64|Linux-arm64) ASSET=bicep-linux-arm64 ;; Linux-x86_64) ASSET=bicep-linux-x64 ;;
      *) echo "no Bicep binary for $(uname -s)-$(uname -m); install the Azure CLI"; exit 1 ;;
    esac
    mkdir -p "$HOME/.azure/bin"
    curl -fsSL -o "$HOME/.azure/bin/bicep" "https://github.com/Azure/bicep/releases/latest/download/$ASSET"
    chmod +x "$HOME/.azure/bin/bicep"
    "$HOME/.azure/bin/bicep" --version
    echo "Install the Azure CLI too for everything after build: https://learn.microsoft.com/cli/azure/install-azure-cli" ;;
  build)
    TMP="$(mktemp -d)"
    bicep_build "$AZ_DIR/main.bicep" "$TMP/main.json"
    bicep_lint "$AZ_DIR/main.bicep"
    echo "built deploy/azure/main.bicep ($(python3 -c 'import json,sys; t=json.load(open(sys.argv[1])); print(len(t["resources"]), "top-level resources,", len(t["outputs"]), "outputs")' "$TMP/main.json")); lint clean"
    rm -rf "$TMP" ;;
  what-if)
    # The review before an apply: what the deployment would create, change or delete. It reads
    # the subscription (and needs the resource group), so it is a call to Azure, not a build.
    group_exists || az group create -n "$RG" -l "$LOCATION" --tags "environment=$ENVIRONMENT" course=ai-engineering -o none
    TMP="$(mktemp -d)"; write_params "$TMP/params.json"
    az deployment group what-if -g "$RG" --template-file "$AZ_DIR/main.bicep" --parameters "@$TMP/params.json"
    rm -rf "$TMP" ;;
  deploy|apply)
    echo "Read deploy/COSTS-platform.md (Azure track). Deploying environment=$ENVIRONMENT mode=$NW_MODE tenants=${NW_TENANTS:-solo} to $RG in $LOCATION."
    az group create -n "$RG" -l "$LOCATION" --tags "environment=$ENVIRONMENT" course=ai-engineering -o none
    TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
    write_params "$TMP/params.json"
    az deployment group create -g "$RG" -n "$ENVIRONMENT-$(date +%Y%m%d%H%M%S)" \
      --template-file "$AZ_DIR/main.bicep" --parameters "@$TMP/params.json" \
      --query properties.outputs -o json > "$TMP/outputs.json"
    "${PARAMS_PY[@]}" outputs "$OUTPUTS" < "$TMP/outputs.json"
    upload_data
    create_indexes
    register_keys
    echo "Next: make images-azure, then make deploy-azure again so the apps run the course images (tag $TAG)."
    echo "Learners: make tenants-azure lists what each tenant got; their .env is in deploy/azure/README.md." ;;
  outputs)
    # outputs.json from the newest deployment in the resource group: what a delivery pipeline (or a
    # second laptop) needs before release, approve or status.
    az deployment group list -g "$RG" --query "sort_by([?properties.outputs.NW_AZURE_ACR && properties.provisioningState=='Succeeded'], &properties.timestamp)[-1].properties.outputs" -o json \
      | "${PARAMS_PY[@]}" outputs "$OUTPUTS" ;;
  tenants)
    SUB="${2:-}"; TENANT="${3:-}"
    case "$SUB" in
      "") "${PARAMS_PY[@]}" tenants ;;
      add)
        [ -n "$TENANT" ] || { echo "usage: deploy_azure.sh tenants add <handle>"; exit 2; }
        export NW_TENANTS="${NW_TENANTS:+$NW_TENANTS,}$TENANT"
        exec "$0" deploy ;;
      remove)
        [ -n "$TENANT" ] || { echo "usage: deploy_azure.sh tenants remove <handle>"; exit 2; }
        # Incremental deployments never delete: remove what the tenant owns, then redeploy without it.
        WS="$(out NW_AZURE_ML_WORKSPACE)"; SCOPE="$(out NW_AZURE_ENDPOINT_SCOPE)"; VAULT="$(out NW_AZURE_KEY_VAULT)"
        for kind in policy agent mcp; do az containerapp delete -n "$ENVIRONMENT-$TENANT-$kind" -g "$RG" --yes -o none 2>/dev/null || true; done
        for kind in triage semantic; do az ml online-endpoint delete -n "nw-$TENANT-$kind-$SCOPE" -g "$RG" -w "$WS" --yes -o none 2>/dev/null || true; done
        az ml schedule delete -n "$ENVIRONMENT-$TENANT-retrain-triage" -g "$RG" -w "$WS" --yes -o none 2>/dev/null || true
        if [ -n "$(out NW_AZURE_APIM_NAME)" ]; then
          az rest --method delete --url "https://management.azure.com/subscriptions/$(out NW_AZURE_SUBSCRIPTION_ID)/resourceGroups/$RG/providers/Microsoft.ApiManagement/service/$(out NW_AZURE_APIM_NAME)/subscriptions/$ENVIRONMENT-$TENANT?api-version=2024-05-01" -o none 2>/dev/null || true
        fi
        for secret in gateway-key api-key; do
          az keyvault secret delete --vault-name "$VAULT" -n "$ENVIRONMENT-$TENANT-$secret" -o none 2>/dev/null || true
        done
        az rest --method delete --url "$(out NW_AZURE_SEARCH_ENDPOINT)/indexes/$ENVIRONMENT-$TENANT-policies?api-version=2024-07-01" --resource https://search.azure.com -o none 2>/dev/null || true
        PRINCIPAL="$(az identity show -n "$ENVIRONMENT-$TENANT-id" -g "$RG" --query principalId -o tsv 2>/dev/null || true)"
        remove_assignments "$PRINCIPAL"
        # The learner's own assignments too, when NW_TENANT_USERS named them.
        remove_assignments "$(python3 -c 'import sys; print(dict(p.split("=",1) for p in sys.argv[1].split(",") if "=" in p).get(sys.argv[2], ""))' "${NW_TENANT_USERS:-}" "$TENANT")"
        az identity delete -n "$ENVIRONMENT-$TENANT-id" -g "$RG" -o none 2>/dev/null || true
        export NW_TENANTS; NW_TENANTS="$(python3 -c 'import sys; print(",".join(t for t in sys.argv[1].split(",") if t and t != sys.argv[2]))' "${NW_TENANTS:-}" "$TENANT")"
        echo "removed $TENANT; redeploying with tenants=$NW_TENANTS"
        exec "$0" deploy ;;
      *) echo "usage: deploy_azure.sh tenants [add|remove <handle>]"; exit 2 ;;
    esac ;;
  status)
    WS="$(out NW_AZURE_ML_WORKSPACE)"
    echo "== Online endpoints"; az ml online-endpoint list -g "$RG" -w "$WS" --query "[].{name:name, traffic:traffic, state:provisioning_state}" -o table
    echo "== Container Apps"; az containerapp list -g "$RG" --query "[].{name:name, revision:properties.latestReadyRevisionName, fqdn:properties.configuration.ingress.fqdn}" -o table
    for app in $(live_apps); do echo "-- $app traffic"; az containerapp ingress traffic show -n "$app" -g "$RG" -o table 2>/dev/null || true; done
    echo "== Model deployments"; az cognitiveservices account deployment list -g "$RG" -n "$(out NW_AZURE_FOUNDRY_ENDPOINT | sed -e 's|https://||' -e 's|\..*||')" --query "[].{name:name, model:properties.model.name, version:properties.model.version, capacity:sku.capacity}" -o table
    echo "== Pipeline jobs (last 5)"; az ml job list -g "$RG" -w "$WS" --max-results 5 --query "[].{name:display_name, status:status, experiment:experiment_name}" -o table
    echo "== Retraining schedules"; az ml schedule list -g "$RG" -w "$WS" --query "[].{name:name, enabled:is_enabled}" -o table 2>/dev/null || true
    echo "== Fired alerts"; az rest --method get --url "$(alerts_url)" \
      --query "value[].{alert:properties.essentials.alertRule, severity:properties.essentials.severity, since:properties.essentials.startDateTime}" -o table ;;
  stop)
    # The meter while idle is the online deployments (VMs), the LiteLLM database and the search
    # and APIM units; the first two can stop, the last two bill until destroy.
    WS="$(out NW_AZURE_ML_WORKSPACE)"
    for e in $(az ml online-endpoint list -g "$RG" -w "$WS" --query "[].name" -o tsv); do
      for d in $(az ml online-deployment list -e "$e" -g "$RG" -w "$WS" --query "[].name" -o tsv); do
        az ml online-deployment delete -n "$d" -e "$e" -g "$RG" -w "$WS" --yes --no-wait -o none && echo "deleting deployment $d on $e"; done; done
    for app in $(az containerapp list -g "$RG" --query "[].name" -o tsv); do
      az containerapp update -n "$app" -g "$RG" --min-replicas 0 -o none && echo "scaled $app to zero when idle"; done
    if [ "$(out NW_AZURE_GATEWAY_KIND)" = "litellm" ]; then
      az postgres flexible-server stop -g "$RG" -n "$ENVIRONMENT-gateway-db-$(out NW_AZURE_NAME_SUFFIX)" -o none && echo "stopped the gateway database"; fi
    echo "Idle cost now: AI Search and API Management units, the registry, storage and logs (deploy/COSTS-platform.md). make destroy-azure stops them." ;;
  start)
    if [ "$(out NW_AZURE_GATEWAY_KIND)" = "litellm" ]; then
      az postgres flexible-server start -g "$RG" -n "$ENVIRONMENT-gateway-db-$(out NW_AZURE_NAME_SUFFIX)" -o none && echo "started the gateway database"; fi
    echo "Endpoints are empty after stop: approving a model version (or the promotion drill) deploys it again." ;;
  destroy)
    SUB="$(az account show --query id -o tsv)"
    KV=""; FOUNDRY=""; APIM=""; WS=""
    if have_outputs; then KV="$(out NW_AZURE_KEY_VAULT)"; FOUNDRY="$(out NW_AZURE_FOUNDRY_ENDPOINT | sed -e 's|https://||' -e 's|\..*||')"; APIM="$(out NW_AZURE_APIM_NAME)"; WS="$(out NW_AZURE_ML_WORKSPACE)"; fi
    # Deleting the group only soft-deletes the Azure ML workspace (14 days, name held); delete it
    # permanently first. Its associated resources go with the group.
    if [ -n "$WS" ] && az ml workspace delete -n "$WS" -g "$RG" --permanently-delete --yes -o none 2>/dev/null; then
      echo "deleted workspace $WS permanently"; fi
    echo "Deleting resource group $RG and everything in it."
    az group delete -n "$RG" --yes
    # Soft delete keeps these names (and, for Key Vault and Foundry, the resource) for days;
    # purge them so a redeploy can reuse the names and nothing lingers.
    if [ -n "$KV" ] && az keyvault purge -n "$KV" -l "$LOCATION" -o none 2>/dev/null; then echo "purged Key Vault $KV"; fi
    if [ -n "$FOUNDRY" ] && az cognitiveservices account purge -g "$RG" -n "$FOUNDRY" -l "$LOCATION" -o none 2>/dev/null; then echo "purged Foundry $FOUNDRY"; fi
    FOUNDRY_EU=""; if have_outputs; then FOUNDRY_EU="$(out NW_AZURE_FOUNDRY_EU_ENDPOINT | sed -e 's|https://||' -e 's|\..*||')"; fi
    if [ -n "$FOUNDRY_EU" ] && az cognitiveservices account purge -g "$RG" -n "$FOUNDRY_EU" -l "${NW_AZURE_EU_LOCATION:-swedencentral}" -o none 2>/dev/null; then echo "purged Foundry $FOUNDRY_EU"; fi
    if [ -n "$APIM" ] && az apim deletedservice purge --service-name "$APIM" -l "$LOCATION" -o none 2>/dev/null; then echo "purged API Management $APIM"; fi
    # The allowed-sizes policy definition lives at subscription scope (its assignment went with
    # the group).
    for id in $(az policy definition list --query "[?policyType=='Custom' && contains(displayName, '($RG)')].name" -o tsv 2>/dev/null); do
      az policy definition delete --name "$id" -o none && echo "deleted policy definition $id"; done
    # Custom role definitions outlive the group they are scoped to.
    for id in $(az role definition list --custom-role-only true --query "[?contains(roleName, '($RG)')].name" -o tsv); do
      az role definition delete --name "$id" --scope "/subscriptions/$SUB/resourceGroups/$RG" -o none 2>/dev/null || \
      az role definition delete --name "$id" -o none 2>/dev/null || true; done
    if [ "${NW_DEFENDER:-false}" = "true" ]; then az security pricing create -n Containers --tier free -o none && echo "Defender for Containers back to Free"; fi
    rm -f "$OUTPUTS"
    echo "Residual: nothing billed in the resource group; deleted blobs are gone with the account." ;;
  release)
    # A release of the live policy and agent apps: the images tagged $TAG are resolved to digests,
    # each digest's signature is verified, and each app gets a new revision at $CANARY percent;
    # `approve` moves it to 100.
    ACR="$(out NW_AZURE_ACR)"; LOGIN="$(out NW_AZURE_ACR_LOGIN_SERVER)"
    for kind in policy agent; do
      APP="$ENVIRONMENT-live-$kind"
      DIGEST="$(az acr repository show -n "$ACR" --image "nw-$kind:$TAG" --query digest -o tsv)"
      verify_image "$LOGIN/nw-$kind@$DIGEST"
      STABLE="$(az containerapp ingress traffic show -n "$APP" -g "$RG" -o json | python3 -c '
import json,sys
rows=[r for r in json.load(sys.stdin) if r.get("weight",0)>0]
print(max(rows,key=lambda r:r["weight"]).get("revisionName",""))')"
      if [ -z "$STABLE" ]; then STABLE="$(az containerapp show -n "$APP" -g "$RG" --query properties.latestReadyRevisionName -o tsv)"; fi
      # Pin the serving revision first, so the new one starts at 0 percent.
      az containerapp ingress traffic set -n "$APP" -g "$RG" --revision-weight "$STABLE=100" -o none
      # NW_IMAGE_DIGEST: the digest the new revision serves, which /version reports.
      az containerapp update -n "$APP" -g "$RG" --image "$LOGIN/nw-$kind@$DIGEST" \
        --set-env-vars "NW_IMAGE_DIGEST=$DIGEST" --revision-suffix "r$(date +%m%d%H%M%S)" -o none
      NEW="$(az containerapp show -n "$APP" -g "$RG" --query properties.latestRevisionName -o tsv)"
      az containerapp ingress traffic set -n "$APP" -g "$RG" --revision-weight "$STABLE=$((100 - CANARY))" "$NEW=$CANARY" -o none
      az containerapp revision label add -n "$APP" -g "$RG" --label canary --revision "$NEW" --yes -o none 2>/dev/null || true
      echo "$APP: $NEW (nw-$kind@$DIGEST) at $CANARY percent, $STABLE at $((100 - CANARY))"
    done
    echo "Watch the alerts ($ENVIRONMENT-live-policy-5xx, $ENVIRONMENT-live-agent-5xx); approve with: make approve-azure REASON=\"...\"" ;;
  approve)
    # The human gate: refuse while a live alert fires, and refuse when the alerts cannot be read
    # (an error or an unreadable answer is not a quiet one). NW_FORCE=1 overrides both.
    if ! FIRED="$(fired_live_alerts)"; then
      if [ "${NW_FORCE:-0}" != "1" ]; then
        echo "could not read the alerts of $RG (Alerts Management, resource group scope), not approving; NW_FORCE=1 overrides" >&2; exit 1; fi
      echo "could not read the alerts of $RG; NW_FORCE=1, approving anyway" >&2; FIRED=""
    fi
    if [ -n "$FIRED" ] && [ "${NW_FORCE:-0}" != "1" ]; then echo "live alerts are firing, not approving: $FIRED"; exit 1; fi
    for APP in $(live_apps); do
      WEIGHTS="$(az containerapp ingress traffic show -n "$APP" -g "$RG" -o json | python3 -c '
import json,sys
rows=[r for r in json.load(sys.stdin) if r.get("revisionName") and r.get("weight",0)>0]
if len(rows)<2: sys.exit(0)
canary=min(rows,key=lambda r:r["weight"])["revisionName"]
out=[]
for r in rows:
    name=r["revisionName"]
    out.append(name+"="+("100" if name==canary else "0"))
print(" ".join(out))')"
      if [ -z "$WEIGHTS" ]; then echo "$APP: no canary waiting"; continue; fi
      # shellcheck disable=SC2086
      az containerapp ingress traffic set -n "$APP" -g "$RG" --revision-weight $WEIGHTS -o none
      az tag update --resource-id "$(az containerapp show -n "$APP" -g "$RG" --query id -o tsv)" --operation merge \
        --tags "nw-approved=$(date -u +%Y-%m-%dT%H:%MZ)" "nw-approval-reason=${REASON:-approved}" -o none
      echo "$APP: $WEIGHTS"
    done ;;
  keys)
    register_keys ;;
  indexes)
    create_indexes ;;
  *) echo "usage: deploy_azure.sh setup|build|what-if|deploy|outputs|tenants|status|stop|start|destroy|release|approve|keys|indexes"; exit 2 ;;
esac
