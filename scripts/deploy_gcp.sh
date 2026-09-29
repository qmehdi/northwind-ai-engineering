#!/usr/bin/env bash
# The Google Cloud platform: one environment in one project (deploy/gcp/platform).
#
#   scripts/deploy_gcp.sh validate|plan|apply|tenants|status|stop|start|destroy|release|approve|keys
#
# NW_GCP_PROJECT, NW_BILLING_ACCOUNT required. NW_MODE=cohort|solo (default cohort),
# NW_TENANTS=alice,bob (cohort), NW_ENVIRONMENT (default northwind), NW_GCP_RUN_REGION
# (default us-central1), NW_IMAGE_TAG (default the git SHA, `-dirty` with changes), NW_ALERT_EMAIL,
# NW_BUDGET_USD, NW_GITHUB_OWNER, NW_GITHUB_APP_INSTALLATION_ID, NW_GITHUB_TOKEN_SECRET_VERSION,
# NW_RAG_BACKEND=managed|vector_search, NW_GATEWAY_DATABASE=true|false.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ACTION="${1:-plan}"
PROJECT="${NW_GCP_PROJECT:?set NW_GCP_PROJECT}"
REGION="${NW_GCP_RUN_REGION:-us-central1}"
ENVIRONMENT="${NW_ENVIRONMENT:-northwind}"
MODE="${NW_MODE:-cohort}"
TENANTS="${NW_TENANTS:-}"
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
tenants_json() { python3 -c 'import sys,json; print(json.dumps([t for t in sys.argv[1].split(",") if t]))' "$TENANTS"; }
project_number() { gcloud projects describe "$PROJECT" --format='value(projectNumber)'; }
cd deploy/gcp/platform
vars() {
  VARS=(-var "project=$PROJECT" -var "project_number=${NW_GCP_PROJECT_NUMBER:-$(project_number)}" -var "region=$REGION"
        -var "environment=$ENVIRONMENT" -var "mode=$MODE" -var "tenants=$(tenants_json)" -var "image_tag=$TAG"
        -var "billing_account=${NW_BILLING_ACCOUNT:?set NW_BILLING_ACCOUNT}" -var "budget_usd=${NW_BUDGET_USD:-300}"
        -var "alert_email=${NW_ALERT_EMAIL:-}" -var "rag_backend=${NW_RAG_BACKEND:-managed}"
        -var "gateway_database=${NW_GATEWAY_DATABASE:-true}" -var "github_owner=${NW_GITHUB_OWNER:-}"
        -var "github_app_installation_id=${NW_GITHUB_APP_INSTALLATION_ID:-0}"
        -var "github_oauth_token_secret_version=${NW_GITHUB_TOKEN_SECRET_VERSION:-}")
}
init() { terraform init -input=false >/dev/null; }
# The image repository may already exist from scripts/images_gcp.sh (images must be pushed
# before the services that run them); adopt it into state instead of failing on "exists".
adopt_repository() {
  if gcloud artifacts repositories describe "$ENVIRONMENT" --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 \
     && ! terraform state show module.delivery.google_artifact_registry_repository.images >/dev/null 2>&1; then
    terraform import "${VARS[@]}" module.delivery.google_artifact_registry_repository.images \
      "projects/$PROJECT/locations/$REGION/repositories/$ENVIRONMENT" >/dev/null && echo "adopted Artifact Registry repository $ENVIRONMENT"
  fi
}
# Every Cloud Run service of the environment (tenant services, gateway, live services).
services() { gcloud run services list --project "$PROJECT" --region "$REGION" --filter="metadata.name~^$ENVIRONMENT-" --format="value(metadata.name)"; }
case "$ACTION" in
  validate)
    terraform fmt -check -recursive "$ROOT/deploy/gcp"
    init; terraform validate ;;
  plan)
    vars; init; adopt_repository; terraform plan -input=false "${VARS[@]}" ;;
  apply|deploy)
    vars; init; terraform validate; adopt_repository
    echo "Read deploy/COSTS-platform.md. Applying environment=$ENVIRONMENT mode=$MODE tenants=${TENANTS:-solo} image tag $TAG to $PROJECT in $REGION."
    terraform plan -input=false -out=tfplan "${VARS[@]}"
    terraform apply -input=false tfplan
    # The weekly scheduler job reads <prefix>/pipelines/retrain-triage.yaml from the artifacts
    # bucket: compile for this image tag and upload for every tenant.
    (cd "$ROOT" && NW_TRACK=gcp NW_GCP_PROJECT="$PROJECT" NW_GCP_RUN_REGION="$REGION" NW_ENVIRONMENT="$ENVIRONMENT" \
      make pipeline-upload-gcp PIPELINE_IMAGE="$REGION-docker.pkg.dev/$PROJECT/$ENVIRONMENT/nw-pipelines:$TAG" TENANTS="$TENANTS")
    echo "Next: scripts/gcp_gateway_keys.sh registers the tenant keys with the gateway (make keys-gcp)." ;;
  tenants)
    terraform output -json tenants | python3 -c '
import json,sys
d=json.load(sys.stdin)
for t,v in d.items():
    print(t)
    for k in ("prefix","agent_engine","pipelines_sa","identity_sa","gateway_key_secret","rag_corpus","pipeline_root"):
        print(f"  {k:22} {v[k]}")
    for s,u in v["services"].items(): print(f"  {s:22} {u}")' ;;
  status)
    echo "== Cloud Run"; gcloud run services list --project "$PROJECT" --region "$REGION" --filter="metadata.name~^$ENVIRONMENT-" --format="table(metadata.name,status.url,status.latestReadyRevisionName)"
    echo "== Agent Engine"; gcloud ai reasoning-engines list --project "$PROJECT" --region "$REGION" --format="table(displayName,name.basename(),updateTime)" 2>/dev/null || true
    echo "== Live endpoints"; gcloud ai endpoints list --project "$PROJECT" --region "$REGION" --filter="displayName~^$ENVIRONMENT-live-" --format="table(displayName,name.basename(),deployedModels[].id)" 2>/dev/null || true
    echo "== Pipeline runs (last 5)"; gcloud ai pipelines list --project "$PROJECT" --region "$REGION" --limit=5 --format="table(displayName,state,createTime)" 2>/dev/null || true
    echo "== Cloud Deploy"; gcloud deploy rollouts list --project "$PROJECT" --region "$REGION" --delivery-pipeline "$ENVIRONMENT-live" --limit=5 --format="table(name.basename(),state,approvalState)" 2>/dev/null || true ;;
  stop)
    # Temporary overrides of what Terraform owns; the next apply restores them.
    for svc in $(services); do
      gcloud run services update "$svc" --project "$PROJECT" --region "$REGION" --min-instances=0 --max-instances=1 --quiet >/dev/null && echo "scaled $svc to zero"; done
    for e in $(gcloud ai endpoints list --project "$PROJECT" --region "$REGION" --filter="displayName~^$ENVIRONMENT-live-" --format="value(name)"); do
      for m in $(gcloud ai endpoints describe "$e" --project "$PROJECT" --region "$REGION" --format="value(deployedModels[].id)" | tr ';' ' '); do
        gcloud ai endpoints undeploy-model "$e" --project "$PROJECT" --region "$REGION" --deployed-model-id="$m" --quiet && echo "undeployed $m from $e"; done; done
    if gcloud sql instances describe "$ENVIRONMENT-gateway-db" --project "$PROJECT" >/dev/null 2>&1; then
      gcloud sql instances patch "$ENVIRONMENT-gateway-db" --project "$PROJECT" --activation-policy NEVER --quiet && echo "stopped $ENVIRONMENT-gateway-db"; fi
    echo "Idle cost now: buckets and the stopped Cloud SQL disk only. Vector Search deployed indexes (rag_backend=vector_search) keep billing; destroy to stop them." ;;
  start)
    if gcloud sql instances describe "$ENVIRONMENT-gateway-db" --project "$PROJECT" >/dev/null 2>&1; then
      gcloud sql instances patch "$ENVIRONMENT-gateway-db" --project "$PROJECT" --activation-policy ALWAYS --quiet && echo "started $ENVIRONMENT-gateway-db"; fi
    for svc in $(services); do
      gcloud run services update "$svc" --project "$PROJECT" --region "$REGION" --min-instances=0 --max-instances=3 --quiet >/dev/null && echo "restored $svc"; done
    echo "Live endpoints are empty after stop; rerun the promotion drill to deploy the live version." ;;
  destroy)
    vars; init
    terraform destroy -input=false "${VARS[@]}"
    echo "Cloud Deploy created the live Cloud Run services outside Terraform; removing them and their revisions."
    for svc in $(gcloud run services list --project "$PROJECT" --region "$REGION" --filter="metadata.name~^$ENVIRONMENT-live-" --format="value(metadata.name)"); do
      gcloud run services delete "$svc" --project "$PROJECT" --region "$REGION" --quiet && echo "deleted $svc"; done ;;
  release)
    # A release from the laptop: the images tagged $TAG are resolved to digests and handed
    # to Cloud Deploy; the rollout stops at the canary until `approve`.
    RENDER="$(mktemp -d)"; LIVE_SA="$(terraform output -raw live_service_account)"
    for f in delivery/skaffold.yaml delivery/run-*.yaml; do
      sed -e "s/@ENVIRONMENT@/$ENVIRONMENT/g" -e "s/@PROJECT@/$PROJECT/g" -e "s/@REGION@/$REGION/g" -e "s/@LIVE_SA@/$LIVE_SA/g" "$f" > "$RENDER/$(basename "$f")"; done
    IMAGES=""
    for s in triage semantic policy agent; do
      DIGEST="$(gcloud artifacts docker images describe "$REGION-docker.pkg.dev/$PROJECT/$ENVIRONMENT/nw-$s:$TAG" --project "$PROJECT" --format='value(image_summary.digest)')"
      IMAGES="${IMAGES:+$IMAGES,}nw-$s=$REGION-docker.pkg.dev/$PROJECT/$ENVIRONMENT/nw-$s@$DIGEST"; done
    gcloud deploy releases create "rel-$(echo "$TAG" | tr -c 'a-z0-9\n' '-')-$(date +%H%M%S)" --project "$PROJECT" --region "$REGION" \
      --delivery-pipeline "$ENVIRONMENT-live" --source "$RENDER" --images "$IMAGES" --deploy-parameters "environment=$ENVIRONMENT"
    echo "Rollout at $(echo "$IMAGES" | tr ',' '\n' | head -1 | cut -d= -f1) ... canary; approve with: make approve-gcp" ;;
  approve)
    ROLLOUT="$(gcloud deploy rollouts list --project "$PROJECT" --region "$REGION" --delivery-pipeline "$ENVIRONMENT-live" --filter="approvalState=NEEDS_APPROVAL" --format="value(name)" --limit=1)"
    [ -n "$ROLLOUT" ] || { echo "no rollout waits for approval"; exit 1; }
    gcloud deploy rollouts approve "$ROLLOUT" --project "$PROJECT" --region "$REGION" --delivery-pipeline "$ENVIRONMENT-live" --release "$(basename "$(dirname "$(dirname "$ROLLOUT")")")" --quiet
    echo "approved $ROLLOUT; the canary advances to 100 percent" ;;
  keys)
    exec "$ROOT/scripts/gcp_gateway_keys.sh" ;;
  *) echo "usage: deploy_gcp.sh validate|plan|apply|tenants|status|stop|start|destroy|release|approve|keys"; exit 2 ;;
esac
