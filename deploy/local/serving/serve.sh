#!/bin/sh
# Wait until the registry has the alias this replica serves, then serve it. A fresh stack has
# no live model yet; `make local-bootstrap` registers one and this loop picks it up.
# The canary replica sets NW_SERVING_FALLBACK_ALIAS=live: with no canary version yet it serves
# the live one at weight 0 (nginx sends it nothing), so it reports healthy instead of looking
# broken. `deploy(version, canary_percent=...)` moves the canary alias and restarts this replica.
set -eu
: "${NW_SERVING_MODEL:=northwind-solo-triage}"
: "${NW_SERVING_ALIAS:=live}"
: "${NW_SERVING_FALLBACK_ALIAS:=}"
: "${MLFLOW_TRACKING_URI:=http://mlflow:5000}"
has_alias() {
  python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('$MLFLOW_TRACKING_URI/api/2.0/mlflow/registered-models/alias?name=$NW_SERVING_MODEL&alias=$1').status == 200 else 1)" 2>/dev/null
}
alias=""
until [ -n "$alias" ]; do
  if has_alias "$NW_SERVING_ALIAS"; then
    alias="$NW_SERVING_ALIAS"
  elif [ -n "$NW_SERVING_FALLBACK_ALIAS" ] && has_alias "$NW_SERVING_FALLBACK_ALIAS"; then
    alias="$NW_SERVING_FALLBACK_ALIAS"
    echo "no models:/$NW_SERVING_MODEL@$NW_SERVING_ALIAS yet: serving @$alias until a canary is deployed"
  else
    echo "waiting for models:/$NW_SERVING_MODEL@$NW_SERVING_ALIAS in the registry"
    sleep 10
  fi
done
echo "serving models:/$NW_SERVING_MODEL@$alias"
exec mlflow models serve -m "models:/$NW_SERVING_MODEL@$alias" --host 0.0.0.0 --port 8000 --env-manager local --workers 1
