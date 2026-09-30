# The Google Cloud platform of the course, one environment in one project (ADR 0008).
#
#   make validate-gcp                       # fmt, init, validate
#   make plan-gcp NW_TENANTS=alice,bob      # cohort mode, one tenant per learner
#   make deploy-gcp NW_MODE=solo            # one tenant named solo
#
# Modules, one per area of Google's GenAI and ML blueprint:
#   identity      tenant identities, custom roles, per-tenant API keys, secret classes
#   data          Cloud Storage with a managed folder per tenant, BigQuery, Dataplex (off by default)
#   tracking      Pipelines service account, weekly retraining schedule (paused)
#   serving       per-tenant Cloud Run services from the registry artifact
#   live          the promoted target: Vertex endpoints, Model Monitoring, live identity
#   prompts       prompt registration (script) and the RAG Engine corpus per tenant
#   agents        Agent Engine per tenant, Model Armor, the agent registry document
#   gateway       LiteLLM on Cloud Run, keys and budgets per tenant
#   delivery      Artifact Registry, Cloud Build triggers, Cloud Deploy with approval
#   observability dashboard, log-based metrics, alerts, budget
#   guardrails    audit logs (400 days), IAM deny on platform secrets, compute quota caps
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
    "cloudquotas.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudtrace.googleapis.com",
    "dataplex.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "orgpolicy.googleapis.com",
    "logging.googleapis.com",
    "modelarmor.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "serviceusage.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ]
  training_cpu_quota = var.training_cpu_quota > 0 ? var.training_cpu_quota : max(16, 8 * length(local.tenants))
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  project            = var.project
  service            = each.value
  disable_on_destroy = false
}

# The API key of the live services and the instructor (nw/auth.py reads NW_API_KEY); every
# tenant's services take their own key (modules/identity). Write-only: never in state.
# scripts/rotate_key.sh adds a new version out of band.
ephemeral "random_password" "api_key" {
  length  = 40
  special = false
}

resource "google_secret_manager_secret" "api_key" {
  project   = var.project
  secret_id = "${var.environment}-api-key"
  labels    = local.labels
  tags      = { (module.identity.secret_class_key) = module.identity.secret_class_values["service"] }
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "api_key" {
  secret                 = google_secret_manager_secret.api_key.id
  secret_data_wo         = ephemeral.random_password.api_key.result
  secret_data_wo_version = tostring(var.secrets_generation)
}

module "identity" {
  source             = "../modules/identity"
  project            = var.project
  environment        = var.environment
  labels             = local.labels
  tenants            = local.tenants
  tenant_members     = var.tenant_members
  secrets_generation = var.secrets_generation
  depends_on         = [google_project_service.apis]
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
  tenants          = local.tenants
  tenant_users     = module.identity.user_accounts
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
  clouddeploy_folder                = module.data.folders["artifacts:clouddeploy"]
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
  source              = "../modules/gateway"
  project             = var.project
  region              = var.region
  environment         = var.environment
  labels              = local.labels
  tenants             = local.tenants
  tenant_users        = module.identity.user_accounts
  secret_class_key    = module.identity.secret_class_key
  secret_class_values = module.identity.secret_class_values
  secrets_generation  = var.secrets_generation
  remote_registry     = module.delivery.remote_registry
  image               = var.gateway_image
  database            = var.gateway_database
  models              = var.gateway_models
  tenant_budget_usd   = var.tenant_budget_usd
  depends_on          = [google_project_service.apis]
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
  roles              = module.identity.roles
  tenant_users       = module.identity.user_accounts
  folders            = module.data.folders
  dataset_id         = module.data.dataset_id
  depends_on         = [google_project_service.apis]
}

module "serving" {
  source              = "../modules/serving"
  project             = var.project
  project_number      = var.project_number
  region              = var.region
  environment         = var.environment
  labels              = local.labels
  tenants             = local.tenants
  registry            = local.registry
  image_tag           = var.image_tag
  api_key_secrets     = module.identity.api_key_secrets
  gateway_url         = module.gateway.url
  gateway_key_secrets = module.gateway.tenant_key_secrets
  artifacts_bucket    = module.data.artifacts_bucket
  folders             = module.data.folders
  roles               = module.identity.roles
  tenant_users        = module.identity.user_accounts
  rag_corpora         = module.prompts.corpora
  public              = var.public_services
  depends_on          = [google_project_service.apis, module.identity, module.delivery]
}

module "live" {
  source             = "../modules/live"
  project            = var.project
  region             = var.region
  environment        = var.environment
  labels             = local.labels
  models             = var.live_models
  endpoint_id_base   = var.endpoint_id_base
  monitoring         = var.model_monitoring
  artifacts_bucket   = module.data.artifacts_bucket
  data_bucket        = module.data.data_bucket
  capture_dataset_id = module.data.capture_dataset_id
  api_key_secret     = google_secret_manager_secret.api_key.secret_id
  gateway_key_secret = module.gateway.live_key_secret
  roles              = module.identity.roles
  tenant_users       = module.identity.user_accounts
  tenants_for_drill  = local.tenants
  folders            = module.data.folders
  approvers          = var.approvers
  depends_on         = [google_project_service.apis]
}

module "prompts" {
  source                     = "../modules/prompts"
  project                    = var.project
  project_number             = var.project_number
  region                     = var.region
  environment                = var.environment
  labels                     = local.labels
  tenants                    = local.tenants
  artifacts_bucket           = module.data.artifacts_bucket
  embedding_model            = var.embedding_model
  rag_backend                = var.rag_backend
  rag_managed_db_tier        = var.rag_managed_db_tier
  rag_unprovision_on_destroy = var.rag_unprovision_on_destroy
  depends_on                 = [google_project_service.apis]
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
  api_key_secrets     = module.identity.api_key_secrets
  gateway_url         = module.gateway.url
  gateway_key_secrets = module.gateway.tenant_key_secrets
  service_urls        = module.serving.urls
  service_names       = module.serving.names
  mcp_urls            = module.serving.mcp_urls
  mcp_names           = module.serving.mcp_names
  artifacts_bucket    = module.data.artifacts_bucket
  folders             = module.data.folders
  roles               = module.identity.roles
  tenant_users        = module.identity.user_accounts
  images_repository   = module.delivery.images_repository_id
  depends_on          = [google_project_service.apis, module.identity, module.delivery]
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

module "guardrails" {
  source                      = "../modules/guardrails"
  project                     = var.project
  project_number              = var.project_number
  region                      = var.region
  environment                 = var.environment
  organization_id             = var.organization_id
  data_access_logs            = var.data_access_logs
  compute_quota_caps          = var.compute_quota_caps
  training_cpu_quota          = local.training_cpu_quota
  serving_cpu_quota           = var.serving_cpu_quota
  allowed_machine_types       = var.allowed_machine_types
  secret_class_key_namespaced = module.identity.secret_class_key_namespaced
  denied_service_accounts = concat(
    values(module.identity.user_accounts),
    values(module.agents.service_accounts),
    values(module.tracking.service_accounts),
    values(module.serving.service_accounts),
    values(module.serving.mcp_service_accounts),
    [module.live.service_account, module.delivery.deployer_service_account, module.delivery.builder_service_account, module.delivery.pr_service_account],
  )
  denied_users = values(module.identity.tenant_members)
  depends_on   = [google_project_service.apis]
}
