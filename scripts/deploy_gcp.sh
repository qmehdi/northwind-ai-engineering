#!/usr/bin/env bash
# The Google Cloud platform: one environment in one project (deploy/gcp/platform).
#
#   scripts/deploy_gcp.sh state-bucket|validate|plan|apply|tenants|status|stop|start|destroy|release|approve|keys
#
# NW_GCP_PROJECT, NW_BILLING_ACCOUNT required. NW_MODE=cohort|solo (default cohort),
# NW_TENANTS=alice,bob (cohort), NW_TENANT_MEMBERS=alice=alice@example.com,bob=bob@example.com
# (the Google accounts that impersonate each tenant identity), NW_ENVIRONMENT (default northwind),
# NW_GCP_RUN_REGION (default us-central1), NW_IMAGE_TAG (default the git SHA, plus a diff hash
# with changes), NW_ALERT_EMAIL, NW_BUDGET_USD, NW_GITHUB_OWNER, NW_GITHUB_APP_INSTALLATION_ID,
# NW_GITHUB_TOKEN_SECRET_VERSION, NW_RAG_BACKEND=managed|vector_search,
# NW_APPROVERS=user:you@example.com,group:... (who may impersonate <environment>-approvers and
# approve the live agent's proposals), NW_GATEWAY_DATABASE=true|false, NW_GCP_ORGANIZATION_ID (deny policy and machine type
# constraint), NW_SECRETS_GENERATION (bump to rotate the generated secrets).
#
# State: a Cloud Storage bucket `<project>-<environment>-tfstate` with object versioning
# (`state-bucket` creates it; NW_TF_STATE_KMS_KEY adds CMEK). NW_TF_STATE=local keeps a local
# state file instead (solo mode on one laptop): the script writes backend_override.tf.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ACTION="${1:-plan}"
PROJECT="${NW_GCP_PROJECT:?set NW_GCP_PROJECT}"
REGION="${NW_GCP_RUN_REGION:-us-central1}"
ENVIRONMENT="${NW_ENVIRONMENT:-northwind}"
MODE="${NW_MODE:-cohort}"
TENANTS="${NW_TENANTS:-}"
# The image tag: the git SHA, plus a hash of the diff for a dirty tree, so a tag always names one
# build (the repository's tags are immutable). Never `latest`; outside git, set NW_IMAGE_TAG.
image_tag() {
  if [ -n "${NW_IMAGE_TAG:-}" ]; then echo "$NW_IMAGE_TAG"; return; fi
  local sha
  if sha="$(git rev-parse --short HEAD 2>/dev/null)"; then
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
      echo "$sha-dirty-$( (git diff HEAD; git ls-files --others --exclude-standard) | shasum | cut -c1-8)"
    else echo "$sha"; fi
  else
    echo "not a git checkout: set NW_IMAGE_TAG" >&2; exit 2
  fi
}
TAG="$(image_tag)"
tenants_json() { python3 -c 'import sys,json; print(json.dumps([t for t in sys.argv[1].split(",") if t]))' "$TENANTS"; }
approvers_json() { python3 -c 'import sys,json; print(json.dumps([m for m in sys.argv[1].split(",") if m]))' "${NW_APPROVERS:-}"; }
members_hcl() { python3 -c 'import sys,json; print("{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in (p.split("=", 1) for p in sys.argv[1].split(",") if "=" in p)) + "}")' "${NW_TENANT_MEMBERS:-}"; }
STATE_BUCKET="${NW_TF_STATE_BUCKET:-$PROJECT-$ENVIRONMENT-tfstate}"
project_number() { gcloud projects describe "$PROJECT" --format='value(projectNumber)'; }
cd deploy/gcp/platform
vars() {
  VARS=(-var "project=$PROJECT" -var "project_number=${NW_GCP_PROJECT_NUMBER:-$(project_number)}" -var "region=$REGION"
        -var "environment=$ENVIRONMENT" -var "mode=$MODE" -var "tenants=$(tenants_json)" -var "image_tag=$TAG"
        -var "billing_account=${NW_BILLING_ACCOUNT:?set NW_BILLING_ACCOUNT}" -var "budget_usd=${NW_BUDGET_USD:-300}"
        -var "alert_email=${NW_ALERT_EMAIL:-}" -var "rag_backend=${NW_RAG_BACKEND:-managed}"
        -var "gateway_database=${NW_GATEWAY_DATABASE:-true}" -var "github_owner=${NW_GITHUB_OWNER:-}"
        -var "github_app_installation_id=${NW_GITHUB_APP_INSTALLATION_ID:-0}"
        -var "github_oauth_token_secret_version=${NW_GITHUB_TOKEN_SECRET_VERSION:-}"
        -var "tenant_members=$(members_hcl)" -var "organization_id=${NW_GCP_ORGANIZATION_ID:-}"
        -var "secrets_generation=${NW_SECRETS_GENERATION:-1}" -var "approvers=$(approvers_json)")
}
init() {
  if [ "${NW_TF_STATE:-gcs}" = local ]; then
    printf 'terraform {\n  backend "local" {}\n}\n' > backend_override.tf
    terraform init -input=false -reconfigure >/dev/null
  else
    rm -f backend_override.tf
    gcloud storage buckets describe "gs://$STATE_BUCKET" --project "$PROJECT" >/dev/null 2>&1 \
      || { echo "no state bucket gs://$STATE_BUCKET: run scripts/deploy_gcp.sh state-bucket (or NW_TF_STATE=local)"; exit 1; }
    terraform init -input=false -reconfigure -backend-config="bucket=$STATE_BUCKET" -backend-config="prefix=$ENVIRONMENT/platform" >/dev/null
  fi
}
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
  state-bucket)
    # Versioned, uniform access, public access prevented; Terraform state holds no secret
    # values (they are write-only), but it maps the whole platform.
    if ! gcloud storage buckets describe "gs://$STATE_BUCKET" --project "$PROJECT" >/dev/null 2>&1; then
      gcloud storage buckets create "gs://$STATE_BUCKET" --project "$PROJECT" --location "$REGION" \
        --uniform-bucket-level-access --public-access-prevention \
        ${NW_TF_STATE_KMS_KEY:+--default-encryption-key="$NW_TF_STATE_KMS_KEY"}
    fi
    gcloud storage buckets update "gs://$STATE_BUCKET" --versioning --project "$PROJECT" >/dev/null
    echo "state bucket gs://$STATE_BUCKET (versioned); prefix $ENVIRONMENT/platform" ;;
  validate)
    terraform fmt -check -recursive "$ROOT/deploy/gcp"
    terraform init -input=false -backend=false >/dev/null; terraform validate ;;
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
    init; terraform output -json tenants | python3 -c '
import json,sys
d=json.load(sys.stdin)
for t,v in d.items():
    print(t)
    for k in ("prefix","identity_sa","agent_engine","pipelines_sa","api_key_secret","gateway_key_secret","mcp_url","rag_corpus","pipeline_root"):
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
    echo "Idle cost now: buckets, the stopped Cloud SQL disk, and the project's RagManagedDb tier (Basic bills every hour until destroy sets it to Unprovisioned). Vector Search deployed indexes (rag_backend=vector_search) keep billing too; destroy to stop them." ;;
  start)
    if gcloud sql instances describe "$ENVIRONMENT-gateway-db" --project "$PROJECT" >/dev/null 2>&1; then
      gcloud sql instances patch "$ENVIRONMENT-gateway-db" --project "$PROJECT" --activation-policy ALWAYS --quiet && echo "started $ENVIRONMENT-gateway-db"; fi
    for svc in $(services); do
      gcloud run services update "$svc" --project "$PROJECT" --region "$REGION" --min-instances=0 --max-instances=3 --quiet >/dev/null && echo "restored $svc"; done
    echo "Live endpoints are empty after stop; rerun the promotion drill to deploy the live version." ;;
  destroy)
    vars; init
    # Terraform undeploys the live endpoints' models and sets RagManagedDb to Unprovisioned on
    # its own (modules/live, modules/prompts); this first pass makes a retry after a failed
    # destroy safe too.
    for e in $(gcloud ai endpoints list --project "$PROJECT" --region "$REGION" --filter="displayName~^$ENVIRONMENT-live-" --format="value(name)" 2>/dev/null); do
      for m in $(gcloud ai endpoints describe "$e" --project "$PROJECT" --region "$REGION" --format="value(deployedModels[].id)" | tr ';' ' '); do
        gcloud ai endpoints undeploy-model "$e" --project "$PROJECT" --region "$REGION" --deployed-model-id="$m" --quiet && echo "undeployed $m from $e"; done; done
    terraform destroy -input=false "${VARS[@]}"
    echo "Cloud Deploy created the live Cloud Run services outside Terraform; removing them and their revisions."
    for svc in $(gcloud run services list --project "$PROJECT" --region "$REGION" --filter="metadata.name~^$ENVIRONMENT-live-" --format="value(metadata.name)"); do
      gcloud run services delete "$svc" --project "$PROJECT" --region "$REGION" --quiet && echo "deleted $svc"; done
    echo "Left behind on purpose: the state bucket gs://$STATE_BUCKET (delete it with gcloud storage rm -r when the environment is gone for good) and Cloud Logging's _Required bucket." ;;
  release)
    # A release from the laptop is the pipeline's own build: Cloud Build builds the images from
    # this checkout as the builder account, signs and verifies every digest, and creates the
    # Cloud Deploy release. Nothing a laptop built reaches live. The rollout then waits for
    # `approve`.
    init
    BUILDER="$(terraform output -raw builder_service_account)"
    LIVE_SA="$(terraform output -raw live_service_account)"
    ARTIFACTS="$(terraform output -json buckets | python3 -c 'import json,sys; print(json.load(sys.stdin)["artifacts"])')"
    (cd "$ROOT" && gcloud builds submit . --project "$PROJECT" --region "$REGION" \
      --config deploy/gcp/platform/delivery/cloudbuild-main.yaml \
      --service-account "projects/$PROJECT/serviceAccounts/$BUILDER" \
      --gcs-source-staging-dir "gs://$ARTIFACTS/clouddeploy/source" \
      --substitutions "_TAG=$TAG,_ENVIRONMENT=$ENVIRONMENT,_REGION=$REGION,_REGISTRY=$REGION-docker.pkg.dev/$PROJECT/$ENVIRONMENT,_PIPELINE=$ENVIRONMENT-live,_LIVE_SA=$LIVE_SA,_DEPLOYER_SA=$ENVIRONMENT-deployer@$PROJECT.iam.gserviceaccount.com,_BUILDER_SA=$BUILDER,_STAGING=gs://$ARTIFACTS/clouddeploy/source")
    echo "Release created; approve with: make approve-gcp" ;;
  approve)
    # One step per call: approve a rollout that waits for approval (before any traffic moves),
    # or advance a rollout whose canary is serving to the stable phase.
    ROLLOUT="$(gcloud deploy rollouts list --project "$PROJECT" --region "$REGION" --delivery-pipeline "$ENVIRONMENT-live" --filter="approvalState=NEEDS_APPROVAL" --format="value(name)" --limit=1)"
    if [ -n "$ROLLOUT" ]; then
      gcloud deploy rollouts approve "$ROLLOUT" --project "$PROJECT" --region "$REGION" --delivery-pipeline "$ENVIRONMENT-live" --release "$(basename "$(dirname "$(dirname "$ROLLOUT")")")" --quiet
      echo "approved $ROLLOUT; the canary deploys at canary_percent, then run make approve-gcp again to advance"; exit 0
    fi
    ROLLOUT="$(gcloud deploy rollouts list --project "$PROJECT" --region "$REGION" --delivery-pipeline "$ENVIRONMENT-live" --filter="state=IN_PROGRESS" --format="value(name)" --limit=1)"
    [ -n "$ROLLOUT" ] || { echo "no rollout waits for approval or for its advance"; exit 1; }
    gcloud deploy rollouts advance "$ROLLOUT" --project "$PROJECT" --region "$REGION" --delivery-pipeline "$ENVIRONMENT-live" --release "$(basename "$(dirname "$(dirname "$ROLLOUT")")")" --phase-id stable --quiet
    echo "advanced $ROLLOUT to stable: 100 percent" ;;
  keys)
    init; exec "$ROOT/scripts/gcp_gateway_keys.sh" ;;
  *) echo "usage: deploy_gcp.sh state-bucket|validate|plan|apply|tenants|status|stop|start|destroy|release|approve|keys"; exit 2 ;;
esac
