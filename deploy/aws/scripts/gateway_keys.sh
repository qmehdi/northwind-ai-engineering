#!/usr/bin/env bash
# Mint (or re-mint) the model gateway virtual key for one owner and store it in Secrets Manager.
#   deploy/aws/scripts/gateway_keys.sh <tenant|live> [budget_usd]
# Reads the master key and the gateway URL from deploy/aws/outputs.json, calls LiteLLM's
# POST /key/generate with the owner's three models and a monthly budget, and writes the key to
# the secret `<environment>-<owner>-gateway-key` (created for `live` by the stack, created here
# for a tenant). Idempotent: a second run replaces the secret value with a fresh key.
set -euo pipefail
cd "$(dirname "$0")/../../.."
OWNER="${1:?usage: gateway_keys.sh <tenant|live> [budget_usd]}"
BUDGET="${2:-${NW_TENANT_BUDGET_USD:-25}}"
OUT=deploy/aws/outputs.json
read -r ENVIRONMENT URL MASTER_ARN <<<"$(python3 -c '
import json,sys
o=json.load(open(sys.argv[1])); o=next(v for k,v in o.items() if k.startswith("northwind"))
print(o["Environment"], o["GatewayUrl"], o["GatewayMasterKeyArn"])' "$OUT")"
MASTER="$(aws secretsmanager get-secret-value --secret-id "$MASTER_ARN" --query SecretString --output text)"
if [ "$OWNER" = "live" ]; then models='["workhorse","judge","economy"]'; else models="[\"$OWNER/workhorse\",\"$OWNER/judge\",\"$OWNER/economy\"]"; fi
KEY="$(curl -sS -X POST "$URL/key/generate" -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d "{\"key_alias\":\"$ENVIRONMENT-$OWNER\",\"team_id\":\"$OWNER\",\"models\":$models,\"max_budget\":$BUDGET,\"budget_duration\":\"30d\",\"metadata\":{\"nw_tenant\":\"$OWNER\"}}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["key"])')"
SECRET="$ENVIRONMENT-$OWNER-gateway-key"
if aws secretsmanager describe-secret --secret-id "$SECRET" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --secret-id "$SECRET" --secret-string "$KEY" >/dev/null
else
  aws secretsmanager create-secret --name "$SECRET" --description "$ENVIRONMENT model gateway virtual key for $OWNER" --secret-string "$KEY" --tags Key=nw:tenant,Value="$OWNER" >/dev/null
fi
echo "$OWNER: virtual key stored in $SECRET (budget $BUDGET USD per 30 days, models $models)"
