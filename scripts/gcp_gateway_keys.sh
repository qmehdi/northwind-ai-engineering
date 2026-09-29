#!/usr/bin/env bash
# Register every tenant's gateway key with the LiteLLM proxy, with its budget and labels.
# Terraform generated the key values (Secret Manager `<environment>-<tenant>-gateway-key`)
# and handed them to the tenant's services; this script tells the proxy about them.
# Idempotent: an existing key gets its budget updated. Needs the gateway database.
#
#   NW_GCP_PROJECT=p scripts/gcp_gateway_keys.sh            # every tenant in terraform output
#   NW_GCP_PROJECT=p scripts/gcp_gateway_keys.sh alice bob   # these tenants
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="${NW_GCP_PROJECT:?set NW_GCP_PROJECT}"
ENVIRONMENT="${NW_ENVIRONMENT:-northwind}"
BUDGET="${NW_TENANT_BUDGET_USD:-25}"
cd deploy/gcp/platform
GATEWAY="${NW_GATEWAY_URL:-$(terraform output -raw gateway_url)}"
MASTER="$(gcloud secrets versions access latest --secret "$ENVIRONMENT-gateway-master-key" --project "$PROJECT")"
if [ "$#" -gt 0 ]; then TENANTS="$*"; else TENANTS="$(terraform output -json tenants | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)))')"; fi
for t in $TENANTS; do
  KEY="$(gcloud secrets versions access latest --secret "$ENVIRONMENT-$t-gateway-key" --project "$PROJECT")"
  if curl -sf -H "Authorization: Bearer $MASTER" "$GATEWAY/key/info?key=$KEY" >/dev/null 2>&1; then
    curl -sf -X POST -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" "$GATEWAY/key/update" \
      -d "{\"key\": \"$KEY\", \"max_budget\": $BUDGET, \"budget_duration\": \"30d\"}" >/dev/null
    echo "updated key for $ENVIRONMENT-$t (budget $BUDGET USD per 30 days)"
  else
    curl -sf -X POST -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" "$GATEWAY/key/generate" \
      -d "{\"key\": \"$KEY\", \"key_alias\": \"$ENVIRONMENT-$t\", \"models\": [\"workhorse\", \"judge\", \"economy\"], \"max_budget\": $BUDGET, \"budget_duration\": \"30d\", \"metadata\": {\"tenant\": \"$t\", \"environment\": \"$ENVIRONMENT\"}}" >/dev/null
    echo "registered key for $ENVIRONMENT-$t (budget $BUDGET USD per 30 days)"
  fi
done
echo "spend per key: curl -H 'Authorization: Bearer <master>' $GATEWAY/spend/keys"
