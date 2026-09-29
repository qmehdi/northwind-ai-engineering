# The Google Cloud platform of the course, one environment in one project (ADR 0008).
#
#   make validate-gcp                       # fmt, init, validate
#   make plan-gcp NW_TENANTS=alice,bob      # cohort mode, one tenant per learner
#   make deploy-gcp NW_MODE=solo            # one tenant named solo
#
# Modules, one per area of Google's GenAI and ML blueprint:
#   data          Cloud Storage, BigQuery, Dataplex (off by default)
#   tracking      Pipelines service account, weekly retraining schedule (paused)
#   serving       per-tenant Cloud Run services from the registry artifact
#   live          the promoted target: Vertex endpoints, Model Monitoring, live identity
#   prompts       prompt registration (script) and the RAG Engine corpus per tenant
#   agents        Agent Engine per tenant, Model Armor, the agent registry document
#   gateway       LiteLLM on Cloud Run, keys and budgets per tenant
#   delivery      Artifact Registry, Cloud Build triggers, Cloud Deploy with approval
#   observability dashboard, log-based metrics, alerts, budget
#
# Vertex AI became the Agent Platform (formerly Vertex AI) on 2026-04-22; the API
# (aiplatform.googleapis.com) and the google_vertex_ai_* resources kept their names.

locals {
  tenants  = var.mode == "solo" ? ["solo"] : var.tenants
  registry = "${var.region}-docker.pkg.dev/${var.project}/${var.environment}"
  labels = {
    environment = var.environment
    course      = "ai-engineering"
  }
  apis = [
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudbuild.googleapis.com",
    "clouddeploy.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudtrace.googleapis.com",
    "dataplex.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "modelarmor.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  project            = var.project
  service            = each.value
  disable_on_destroy = false
}

# One cohort API key for every service (nw/auth.py reads NW_API_KEY); scripts/rotate_key.sh
# adds a new version out of band.
resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "google_secret_manager_secret" "api_key" {
  project   = var.project
  secret_id = "${var.environment}-api-key"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "api_key" {
  secret      = google_secret_manager_secret.api_key.id
  secret_data = random_password.api_key.result
}

module "data" {
  source           = "../modules/data"
  project          = var.project
  region           = var.region
  environment      = var.environment
  labels           = local.labels
  cmek_key         = var.cmek_key
  dataplex_catalog = var.dataplex_catalog
  tickets_path     = "${path.module}/../../../data/tickets.jsonl"
  golden_dir       = "${path.module}/../../../data/golden"
  depends_on       = [google_project_service.apis]
}

module "delivery" {
  source                            = "../modules/delivery"
  project                           = var.project
  project_number                    = var.project_number
  region                            = var.region
  environment                       = var.environment
  labels                            = local.labels
  artifacts_bucket                  = module.data.artifacts_bucket
  live_service_account              = module.live.service_account
  github_owner                      = var.github_owner
  github_repo                       = var.github_repo
  github_branch                     = var.github_branch
  github_app_installation_id        = var.github_app_installation_id
  github_oauth_token_secret_version = var.github_oauth_token_secret_version
  canary_percent                    = var.canary_percent
  depends_on                        = [google_project_service.apis]
}

module "gateway" {
  source            = "../modules/gateway"
  project           = var.project
  region            = var.region
  environment       = var.environment
  labels            = local.labels
  tenants           = local.tenants
  tenant_members    = var.tenant_members
  remote_registry   = module.delivery.remote_registry
  image             = var.gateway_image
  database          = var.gateway_database
  models            = var.gateway_models
  tenant_budget_usd = var.tenant_budget_usd
  depends_on        = [google_project_service.apis]
}

module "tracking" {
  source             = "../modules/tracking"
  project            = var.project
  region             = var.region
  environment        = var.environment
  labels             = local.labels
  tenants            = local.tenants
  data_bucket        = module.data.data_bucket
  artifacts_bucket   = module.data.artifacts_bucket
  pipelines_bucket   = module.data.pipelines_bucket
  production_summary = module.data.production_summaries["triage"]
  scheduler_enabled  = var.scheduler_enabled
  scheduler_cron     = var.scheduler_cron
  depends_on         = [google_project_service.apis]
}

module "serving" {
  source              = "../modules/serving"
  project             = var.project
  region              = var.region
  environment         = var.environment
  labels              = local.labels
  tenants             = local.tenants
  registry            = local.registry
  image_tag           = var.image_tag
  api_key_secret      = google_secret_manager_secret.api_key.secret_id
  gateway_url         = module.gateway.url
  gateway_key_secrets = module.gateway.tenant_key_secrets
  artifacts_bucket    = module.data.artifacts_bucket
  rag_corpora         = module.prompts.corpora
  public              = var.public_services
  depends_on          = [google_project_service.apis, google_secret_manager_secret_version.api_key, module.delivery]
}

module "live" {
  source           = "../modules/live"
  project          = var.project
  region           = var.region
  environment      = var.environment
  labels           = local.labels
  models           = var.live_models
  endpoint_id_base = var.endpoint_id_base
  monitoring       = var.model_monitoring
  artifacts_bucket = module.data.artifacts_bucket
  data_bucket      = module.data.data_bucket
  api_key_secret   = google_secret_manager_secret.api_key.secret_id
  depends_on       = [google_project_service.apis]
}

module "prompts" {
  source              = "../modules/prompts"
  project             = var.project
  project_number      = var.project_number
  region              = var.region
  environment         = var.environment
  labels              = local.labels
  tenants             = local.tenants
  artifacts_bucket    = module.data.artifacts_bucket
  embedding_model     = var.embedding_model
  rag_backend         = var.rag_backend
  rag_managed_db_tier = var.rag_managed_db_tier
  depends_on          = [google_project_service.apis]
}

module "agents" {
  source              = "../modules/agents"
  project             = var.project
  project_number      = var.project_number
  region              = var.region
  environment         = var.environment
  labels              = local.labels
  tenants             = local.tenants
  registry            = local.registry
  image_tag           = var.image_tag
  api_key_secret      = google_secret_manager_secret.api_key.secret_id
  gateway_url         = module.gateway.url
  gateway_key_secrets = module.gateway.tenant_key_secrets
  service_urls        = module.serving.urls
  service_names       = module.serving.names
  artifacts_bucket    = module.data.artifacts_bucket
  depends_on          = [google_project_service.apis, google_secret_manager_secret_version.api_key, module.delivery]
}

module "observability" {
  source          = "../modules/observability"
  project         = var.project
  region          = var.region
  environment     = var.environment
  tenants         = local.tenants
  alert_email     = var.alert_email
  billing_account = var.billing_account
  budget_usd      = var.budget_usd
  gateway_service = module.gateway.service_name
  live_endpoints  = module.live.endpoint_ids
  depends_on      = [google_project_service.apis]
}
