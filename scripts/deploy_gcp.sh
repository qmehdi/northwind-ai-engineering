#!/usr/bin/env bash
# GCP: validate, plan, apply. TIER=session|reference.
# NW_IMAGE_TAG defaults to the git SHA (`-dirty` when the tree has changes; `latest` outside
# git), the same default scripts/images_gcp.sh pushes, so a revision names the code it runs.
# NW_CANARY=10 sends 10 percent of traffic to the newest revision and keeps 90 on the one
# serving before, tagged `stable`. NW_STAGE=<word> prefixes every name with northwind-<word>.
set -euo pipefail
cd "$(dirname "$0")/.."
TIER="${TIER:-session}"
ACTION="${1:-deploy}"
PROJECT="${NW_GCP_PROJECT:?set NW_GCP_PROJECT}"
REGION="${NW_GCP_RUN_REGION:-us-central1}"
STAGE="${NW_STAGE:-}"
PREFIX="northwind${STAGE:+-$STAGE}"
CANARY="${NW_CANARY:-0}"
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
# stop and start are temporary overrides of the scaling Terraform owns. stop pins every
# northwind service to min 0, max 1; start puts max back to the service module's default,
# read from the module so the two never drift. The next terraform apply restores the same
# values anyway, so neither action leaves state that fights the plan.
MODULE_MAX=$(sed -n '/variable "max_instances"/,/}/p' deploy/gcp/modules/service/main.tf | sed -n 's/.*default *= *\([0-9][0-9]*\).*/\1/p')
MODULE_MAX="${MODULE_MAX:-3}"
# Exact names, so `stop` with no stage leaves a staged deployment alone.
services() { gcloud run services list --project "$PROJECT" --region "$REGION" --filter="metadata.name~^$PREFIX-(triage|semantic|policy|agent|mcp)\$" --format="value(metadata.name)"; }
case "$ACTION" in
  stop)
    for svc in $(services); do
      gcloud run services update "$svc" --project "$PROJECT" --region "$REGION" --min-instances=0 --max-instances=1 --quiet >/dev/null && echo "scaled $svc to zero"; done
    exit 0 ;;
  start)
    for svc in $(services); do
      gcloud run services update "$svc" --project "$PROJECT" --region "$REGION" --min-instances=0 --max-instances="$MODULE_MAX" --quiet >/dev/null && echo "restored $svc to max $MODULE_MAX"; done
    exit 0 ;;
esac
cd "deploy/gcp/$TIER"
VARS=(-var "project=$PROJECT" -var "region=$REGION" -var "image_tag=$TAG" -var "stage=$STAGE")
if [ "$TIER" = session ]; then
  VARS+=(-var "billing_account=${NW_BILLING_ACCOUNT:?set NW_BILLING_ACCOUNT}" -var "budget_usd=${NW_BUDGET_USD:-100}" -var "alert_email=${NW_ALERT_EMAIL:-}" -var "canary_percent=$CANARY")
fi
# No -upgrade: the lock file pins the provider and CI installs from it.
terraform init -input=false >/dev/null
case "$ACTION" in
  validate) terraform validate ;;
  plan)     terraform plan -input=false "${VARS[@]}" ;;
  deploy)
    terraform validate
    echo "Read deploy/COSTS.md. Applying tier=$TIER stage=${STAGE:-default} image tag $TAG to project $PROJECT in $REGION."
    if [ "$TIER" = session ] && [ "$CANARY" != 0 ]; then
      echo "Canary: the newest revision takes $CANARY percent under the tag latest; the revision serving now keeps the rest under stable."
    fi
    terraform plan -input=false -out=tfplan "${VARS[@]}"
    terraform apply -input=false tfplan ;;
  destroy)
    terraform destroy -input=false "${VARS[@]}"
    # The image repository is created by scripts/images_gcp.sh, outside Terraform, so the
    # session destroy removes it here (images included) and nothing is left behind. Stages
    # share the repository, so only the unstaged destroy deletes it.
    if [ "$TIER" = session ] && [ -z "$STAGE" ]; then
      gcloud artifacts repositories delete northwind --location "$REGION" --project "$PROJECT" --quiet 2>/dev/null \
        && echo "deleted Artifact Registry repository northwind" || echo "Artifact Registry repository northwind already gone"
    fi ;;
  *) echo "usage: deploy_gcp.sh validate|plan|deploy|stop|start|destroy"; exit 2 ;;
esac
