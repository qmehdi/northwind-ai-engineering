#!/bin/sh
# Wait until the registry has the alias this replica serves, then serve it. A fresh stack has
# no live model yet; `make local-bootstrap` registers one and this loop picks it up.
set -eu
: "${NW_SERVING_MODEL:=northwind-solo-triage}"
: "${NW_SERVING_ALIAS:=live}"
: "${MLFLOW_TRACKING_URI:=http://mlflow:5000}"
url="$MLFLOW_TRACKING_URI/api/2.0/mlflow/registered-models/alias?name=$NW_SERVING_MODEL&alias=$NW_SERVING_ALIAS"
until python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('$url').status == 200 else 1)" 2>/dev/null; do
  echo "waiting for models:/$NW_SERVING_MODEL@$NW_SERVING_ALIAS in the registry"
  sleep 10
done
echo "serving models:/$NW_SERVING_MODEL@$NW_SERVING_ALIAS"
exec mlflow models serve -m "models:/$NW_SERVING_MODEL@$NW_SERVING_ALIAS" --host 0.0.0.0 --port 8000 --env-manager local --workers 1
