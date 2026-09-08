#!/usr/bin/env bash
# GCP: validate, plan, apply. TIER=session|reference.
set -euo pipefail
cd "$(dirname "$0")/.."
TIER="${TIER:-session}"
ACTION="${1:-deploy}"
PROJECT="${NW_GCP_PROJECT:?set NW_GCP_PROJECT}"
REGION="${NW_GCP_RUN_REGION:-us-central1}"
cd "deploy/gcp/$TIER"
VARS=(-var "project=$PROJECT" -var "region=$REGION" -var "image_tag=${NW_IMAGE_TAG:-latest}")
if [ "$TIER" = session ]; then
  VARS+=(-var "billing_account=${NW_BILLING_ACCOUNT:?set NW_BILLING_ACCOUNT}" -var "budget_usd=${NW_BUDGET_USD:-100}" -var "alert_email=${NW_ALERT_EMAIL:-}")
fi
terraform init -input=false -upgrade >/dev/null
case "$ACTION" in
  validate) terraform validate ;;
  plan)     terraform plan -input=false "${VARS[@]}" ;;
  deploy)
    terraform validate
    echo "Read deploy/COSTS.md. Applying tier=$TIER to project $PROJECT in $REGION."
    terraform plan -input=false -out=tfplan "${VARS[@]}"
    terraform apply -input=false tfplan ;;
  destroy)  terraform destroy -input=false "${VARS[@]}" ;;
  stop)
    for svc in $(gcloud run services list --project "$PROJECT" --region "$REGION" --filter="metadata.name~^northwind-" --format="value(metadata.name)"); do
      gcloud run services update "$svc" --project "$PROJECT" --region "$REGION" --min-instances=0 --max-instances=1 --quiet >/dev/null && echo "scaled $svc to zero"; done ;;
  start)
    for svc in $(gcloud run services list --project "$PROJECT" --region "$REGION" --filter="metadata.name~^northwind-" --format="value(metadata.name)"); do
      gcloud run services update "$svc" --project "$PROJECT" --region "$REGION" --max-instances=3 --quiet >/dev/null && echo "restored $svc"; done ;;
  *) echo "usage: deploy_gcp.sh validate|plan|deploy|stop|start|destroy"; exit 2 ;;
esac
