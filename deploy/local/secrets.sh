#!/bin/sh
# The Local stack's secrets, generated on the first `make local-up` and never committed.
#
#   deploy/local/secrets.sh          # ensure: write what is missing, change nothing that exists
#
# It appends a block to `.env` (untracked; Docker Compose reads it for every `docker compose`
# call, so `make up`, `make local-*` and nw.platform.local all see the same values):
#   LITELLM_MASTER_KEY    the gateway's master key
#   NW_DB_PASSWORD        Postgres (MLflow's store and LiteLLM's keys and spend)
#   NW_S3_ACCESS_KEY, NW_S3_SECRET_KEY   RustFS, MLflow's artifact store
#   NW_GRAFANA_PASSWORD   Grafana's admin
#   NW_GATEWAY_KEY        your tenant's gateway key (NW_TENANT, default solo)
# and writes one gateway key per tenant of deploy/local/litellm/tenants.tsv into
# deploy/local/litellm/tenant-keys.tsv (untracked), which the litellm-keys job registers.
# A value already set in `.env` is kept: set your own before the first start if you prefer.
#
# A stack started before this script existed initialised its volumes with the old published
# defaults. Postgres, RustFS and Grafana keep the credentials they were initialised with, so
# when those volumes exist the block records the old values (the running stack keeps working)
# and says so; `make local-down`, `docker volume rm northwind_postgres-data
# northwind_rustfs-data northwind_grafana-data`, deleting the block from `.env` and
# `make local-up` starts over with generated values.
#
# The stack has no authentication beyond these (deploy/local/README.md): every port is bound
# to 127.0.0.1, so only this machine reaches it.
set -eu
cd "$(dirname "$0")/../.."

ENV_FILE=.env
KEYS_FILE=deploy/local/litellm/tenant-keys.tsv
TENANTS_FILE=deploy/local/litellm/tenants.tsv
PROJECT="${COMPOSE_PROJECT_NAME:-northwind}"

random() { # characters of hex
  od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

has_key() { # name: set in .env (commented lines do not count)
  [ -f "$ENV_FILE" ] && grep -q "^$1=" "$ENV_FILE"
}

env_value() { # name: its value in .env
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

legacy=0
for v in postgres-data rustfs-data grafana-data; do
  if docker volume inspect "${PROJECT}_$v" >/dev/null 2>&1; then legacy=1; fi
done

tenant_key() { # tenant
  if [ "$legacy" = "1" ]; then echo "sk-nw-$1-change-me"; else echo "sk-nw-$1-$(random 16)"; fi
}

# ----- per-tenant gateway keys ------------------------------------------------------------
touch "$KEYS_FILE"
chmod 600 "$KEYS_FILE"
grep -v '^#' "$TENANTS_FILE" | grep -v '^[[:space:]]*$' | while IFS="$(printf '\t')" read -r name _budget key; do
  if ! grep -q "^$name	" "$KEYS_FILE"; then
    # A third column in tenants.tsv (the old format, or a key you chose) wins.
    printf '%s\t%s\n' "$name" "${key:-$(tenant_key "$name")}" >> "$KEYS_FILE"
  fi
done

# ----- the .env block -----------------------------------------------------------------------
block=""
add() { # name value
  if ! has_key "$1"; then block="$block$1=$2
"; fi
}
if [ "$legacy" = "1" ]; then
  add LITELLM_MASTER_KEY sk-nw-master-change-me
  add NW_DB_PASSWORD northwind
  add NW_S3_ACCESS_KEY nw
  add NW_S3_SECRET_KEY northwind-secret
  add NW_GRAFANA_PASSWORD northwind
else
  add LITELLM_MASTER_KEY "sk-nw-master-$(random 20)"
  add NW_DB_PASSWORD "$(random 16)"
  add NW_S3_ACCESS_KEY "nw$(random 6)"
  add NW_S3_SECRET_KEY "$(random 20)"
  add NW_GRAFANA_PASSWORD "$(random 12)"
fi
tenant="${NW_TENANT:-}"
if [ -z "$tenant" ] && has_key NW_TENANT; then tenant="$(env_value NW_TENANT)"; fi
tenant="${tenant:-solo}"
add NW_GATEWAY_KEY "$(sed -n "s/^$tenant	//p" "$KEYS_FILE" | head -n 1)"

if [ -n "$block" ]; then
  [ -f "$ENV_FILE" ] || : > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  if [ "$legacy" = "1" ]; then
    note="# The volumes of an earlier stack exist, so these are the old defaults it was initialised
# with; deploy/local/secrets.sh says how to start over with generated values."
  else
    note="# Generated; keep them out of the repository (.env is ignored)."
  fi
  {
    echo
    echo "# ----- Local stack secrets, written by deploy/local/secrets.sh on $(date -u +%Y-%m-%d) -----"
    echo "$note"
    printf '%s' "$block"
  } >> "$ENV_FILE"
  echo "local secrets: wrote $(printf '%s' "$block" | grep -c '=') values to .env and the tenant keys to $KEYS_FILE"
  if [ "$legacy" = "1" ]; then
    echo "local secrets: an earlier stack's volumes exist, so .env records its old default credentials;" >&2
    echo "  to replace them: make local-down, remove the postgres, rustfs and grafana volumes, delete the block, make local-up" >&2
  fi
fi
