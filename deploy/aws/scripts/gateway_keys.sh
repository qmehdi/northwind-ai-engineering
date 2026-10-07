#!/usr/bin/env bash
# Mint (or re-mint) the model gateway virtual key for one owner and store it in Secrets Manager.
#   deploy/aws/scripts/gateway_keys.sh <tenant|live> [budget_usd]
# Reads the master key and the gateway URL from deploy/aws/outputs.json, calls LiteLLM's
# POST /key/generate with the owner's three models and a monthly budget, and writes the key to
# the secret `<environment>-<owner>-gateway-key`. The stack owns that secret for every tenant and
# for `live`, so removing a tenant or destroying the platform deletes it; this script only
# writes its value. A second run revokes the previous key first (POST /key/delete; LiteLLM keeps
# key aliases unique) and stores a fresh one. The gateway URL is the HTTPS CloudFront address.
# The client names models by id (`nw/config.py`: the `Models` output, and `eu/<id>` for an EU
# account, the `EuModels` output), so the key carries per-key aliases from those ids to the
# owner's own gateway models (`<owner>/<role>` on its inference profiles, `eu/<owner>/<role>`),
# and may call only those.
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
read -r models aliases <<<"$(python3 - "$OUT" "$OWNER" <<'PY'
import json, sys
o = json.load(open(sys.argv[1])); o = next(v for k, v in o.items() if k.startswith("northwind"))
owner = sys.argv[2]
pairs = lambda text: dict(p.split("=", 1) for p in text.split(",") if "=" in p)
own = (lambda role: role) if owner == "live" else (lambda role: f"{owner}/{role}")
aliases = {model_id: own(role) for role, model_id in pairs(o.get("Models", "")).items()}
aliases.update({f"eu/{m}": f"eu/{own(r)}" for r, m in pairs(o.get("EuModels", "")).items()})
models = sorted({*aliases.values(), *aliases.keys()})
print(json.dumps(models, separators=(",", ":")), json.dumps(aliases, separators=(",", ":")))
PY
)"
SECRET="$ENVIRONMENT-$OWNER-gateway-key"
aws secretsmanager describe-secret --secret-id "$SECRET" >/dev/null \
  || { echo "no secret $SECRET: the stack creates it for every tenant; redeploy with the tenant first"; exit 1; }
OLD="$(aws secretsmanager get-secret-value --secret-id "$SECRET" --query SecretString --output text 2>/dev/null || true)"
if [ -n "$OLD" ]; then
  curl -sS -X POST "$URL/key/delete" -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"keys\":[\"$OLD\"]}" >/dev/null || echo "note: the previous key was not revoked (it may be gone already)"
fi
# The key carries team_id=$OWNER; LiteLLM rejects every request if that team does not exist.
curl -sS -X POST "$URL/team/new" -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d "{\"team_id\":\"$OWNER\",\"team_alias\":\"$OWNER\"}" >/dev/null || true
KEY="$(curl -sS --fail -X POST "$URL/key/generate" -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d "{\"key_alias\":\"$ENVIRONMENT-$OWNER\",\"team_id\":\"$OWNER\",\"models\":$models,\"aliases\":$aliases,\"max_budget\":$BUDGET,\"budget_duration\":\"30d\",\"metadata\":{\"nw_tenant\":\"$OWNER\"}}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["key"])')"
aws secretsmanager put-secret-value --secret-id "$SECRET" --secret-string "$KEY" >/dev/null
echo "$OWNER: virtual key stored in $SECRET (budget $BUDGET USD per 30 days, models $models)"
