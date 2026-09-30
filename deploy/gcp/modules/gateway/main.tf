# The model gateway: LiteLLM on Cloud Run, the one door every model call goes through
# (ADR 0008). The master key lives in Secret Manager; each tenant gets a virtual key with a
# budget, generated here so the services can be handed it at apply time and registered with
# the proxy by scripts/gcp_gateway_keys.sh once the proxy is up. Virtual keys need Postgres,
# so Cloud SQL (db-f1-micro) is created unless `database` is false, in which case only the
# master key works.
#
# Secrets are write-only: the values come from ephemeral random passwords and go to Secret
# Manager through `secret_data_wo` (and to Cloud SQL through `password_wo`), so neither the
# state nor a plan file holds them. `secrets_generation` rotates the master key, the database
# password and the tenant keys on the next apply; the salt key never rotates (LiteLLM encrypts
# stored credentials with it). The platform secrets carry the tag class `platform`, which the
# IAM deny policy in modules/guardrails keeps from every tenant and workload identity.
#
# The image is pinned by digest (variable `gateway_image` in the root); this container holds the
# master key, the database credentials and the model credentials.
variable "project" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "tenants" { type = list(string) }
variable "tenant_users" {
  type        = map(string)
  description = "Tenant to tenant identity email (modules/identity), which may read its own gateway key"
}
variable "secret_class_key" { type = string }
variable "secret_class_values" { type = map(string) }
variable "secrets_generation" { type = number }
variable "remote_registry" { type = string }
variable "image" { type = string }
variable "database" { type = bool }
variable "models" {
  type = map(object({
    model    = string
    location = string
  }))
}
variable "tenant_budget_usd" { type = number }

locals {
  service = "${var.environment}-gateway"
  config = yamlencode({
    model_list = [
      for role, m in var.models : {
        model_name = role
        litellm_params = {
          model              = m.model
          vertex_ai_project  = var.project
          vertex_ai_location = m.location
        }
        model_info = { role = role, environment = var.environment }
      }
    ]
    litellm_settings = {
      drop_params              = true
      request_timeout          = 120
      num_retries              = 2
      success_callback         = []
      turn_off_message_logging = true
    }
    general_settings = merge({
      master_key = "os.environ/LITELLM_MASTER_KEY"
      }, var.database ? {
      database_url                     = "os.environ/DATABASE_URL"
      store_model_in_db                = false
      disable_spend_logs               = false
      allow_requests_on_db_unavailable = false
      # Spend logs are operational data: 90 days (docs/governance retention table).
      maximum_spend_logs_retention_period = "90d"
    } : {})
  })
}

resource "google_service_account" "gateway" {
  project      = var.project
  account_id   = local.service
  display_name = "${var.environment} model gateway"
}

# The gateway is the one identity that calls the models (roles/aiplatform.user covers the
# publisher models' predict); no tenant can change what it runs.
resource "google_project_iam_member" "gateway" {
  for_each = toset(concat(["roles/aiplatform.user", "roles/logging.logWriter", "roles/monitoring.metricWriter"], var.database ? ["roles/cloudsql.client"] : []))
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.gateway.email}"
}

# ----- secrets ------------------------------------------------------------------------------

ephemeral "random_password" "master" {
  length  = 40
  special = false
}

ephemeral "random_password" "salt" {
  length  = 32
  special = false
}

ephemeral "random_password" "db" {
  length  = 32
  special = false
}

locals {
  # The names are plain; the values are ephemeral and reach only write-only arguments.
  secret_names = concat(["master-key", "salt-key", "config"], var.database ? ["database-url"] : [])
  secret_values = merge({
    "master-key" = "sk-${ephemeral.random_password.master.result}"
    "salt-key"   = ephemeral.random_password.salt.result
    "config"     = local.config
    }, var.database ? {
    "database-url" = "postgresql://litellm:${ephemeral.random_password.db.result}@localhost/litellm?host=/cloudsql/${var.project}:${var.region}:${local.service}-db"
  } : {})
  # The salt key is written once and never rotated; the config follows its content.
  secret_generations = {
    "master-key"   = tostring(var.secrets_generation)
    "salt-key"     = "1"
    "config"       = sha256(local.config)
    "database-url" = tostring(var.secrets_generation)
  }
}

resource "google_secret_manager_secret" "gateway" {
  for_each  = toset(local.secret_names)
  project   = var.project
  secret_id = "${local.service}-${each.key}"
  labels    = merge(var.labels, { area = "gateway" })
  tags      = { (var.secret_class_key) = var.secret_class_values["platform"] }
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "gateway" {
  for_each               = toset(local.secret_names)
  secret                 = google_secret_manager_secret.gateway[each.key].id
  secret_data_wo         = local.secret_values[each.key]
  secret_data_wo_version = local.secret_generations[each.key]
}

resource "google_secret_manager_secret_iam_member" "gateway" {
  for_each  = toset(local.secret_names)
  project   = var.project
  secret_id = google_secret_manager_secret.gateway[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

# ----- Cloud SQL for virtual keys, budgets and spend logs -----------------------------------

resource "google_sql_database_instance" "gateway" {
  count               = var.database ? 1 : 0
  project             = var.project
  name                = "${local.service}-db"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = false
  settings {
    tier              = "db-f1-micro"
    edition           = "ENTERPRISE"
    availability_type = "ZONAL"
    disk_size         = 10
    disk_autoresize   = false
    user_labels       = merge(var.labels, { area = "gateway" })
    # Reached only over the Cloud SQL connector from Cloud Run: no authorized networks, and
    # no private IP because the course creates no VPC.
    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }
    backup_configuration {
      enabled = false
    }
  }
}

resource "google_sql_database" "litellm" {
  count    = var.database ? 1 : 0
  project  = var.project
  instance = google_sql_database_instance.gateway[0].name
  name     = "litellm"
}

resource "google_sql_user" "litellm" {
  count    = var.database ? 1 : 0
  project  = var.project
  instance = google_sql_database_instance.gateway[0].name
  name     = "litellm"
  # Same ephemeral value as the database-url secret, in the same apply.
  password_wo         = ephemeral.random_password.db.result
  password_wo_version = var.secrets_generation
}

# ----- the proxy on Cloud Run ---------------------------------------------------------------

resource "google_cloud_run_v2_service" "gateway" {
  project             = var.project
  name                = local.service
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false
  labels              = merge(var.labels, { area = "gateway" })

  template {
    service_account                  = google_service_account.gateway.email
    timeout                          = "300s"
    max_instance_request_concurrency = 40
    labels                           = merge(var.labels, { area = "gateway" })

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    volumes {
      name = "config"
      secret {
        secret = google_secret_manager_secret.gateway["config"].secret_id
        items {
          version = "latest"
          path    = "config.yaml"
        }
      }
    }

    dynamic "volumes" {
      for_each = var.database ? [1] : []
      content {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.gateway[0].connection_name]
        }
      }
    }

    containers {
      image = "${var.remote_registry}/${var.image}" # repository@sha256:digest
      args  = ["--config", "/config/config.yaml", "--port", "4000"]

      ports {
        container_port = 4000
      }

      resources {
        limits            = { cpu = "1", memory = "2Gi" }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      volume_mounts {
        name       = "config"
        mount_path = "/config"
      }

      dynamic "volume_mounts" {
        for_each = var.database ? [1] : []
        content {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }

      dynamic "env" {
        for_each = {
          VERTEXAI_PROJECT      = var.project
          VERTEXAI_LOCATION     = var.region
          STORE_MODEL_IN_DB     = "False"
          LITELLM_LOG           = "INFO"
          DISABLE_SCHEMA_UPDATE = var.database ? "False" : "True"
        }
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = merge({
          LITELLM_MASTER_KEY = "master-key"
          LITELLM_SALT_KEY   = "salt-key"
        }, var.database ? { DATABASE_URL = "database-url" } : {})
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.gateway[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        initial_delay_seconds = 10
        period_seconds        = 10
        failure_threshold     = 30 # Prisma migrates the schema on first start
        timeout_seconds       = 5
        http_get {
          path = "/health/readiness"
          port = 4000
        }
      }

      liveness_probe {
        period_seconds    = 30
        failure_threshold = 3
        http_get {
          path = "/health/liveliness"
          port = 4000
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_version.gateway, google_secret_manager_secret_iam_member.gateway, google_sql_user.litellm]
}

# Keys are the guard: the proxy answers everyone, and a request without a valid key is 401.
# Put Identity-Aware Proxy in front when the cohort has a Google Workspace (README).
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.gateway.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ----- per tenant: a virtual key --------------------------------------------------------------
# The key value is generated here (ephemeral, write-only) so Terraform can hand the secret to
# the tenant's services and agent by reference; scripts/gcp_gateway_keys.sh reads it from Secret
# Manager and registers the same value with the proxy (POST /key/generate accepts a
# caller-chosen key) with the tenant's budget and labels. `live` is the key of the live services
# that Cloud Deploy creates (deploy/gcp/platform/delivery/run-*.yaml).

locals {
  key_owners = concat(var.tenants, ["live"])
}

ephemeral "random_password" "tenant_key" {
  for_each = toset(local.key_owners)
  length   = 32
  special  = false
}

resource "google_secret_manager_secret" "tenant_key" {
  for_each  = toset(local.key_owners)
  project   = var.project
  secret_id = "${var.environment}-${each.key}-gateway-key"
  labels    = merge(var.labels, { area = "gateway", tenant = each.key })
  tags      = { (var.secret_class_key) = var.secret_class_values["service"] }
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "tenant_key" {
  for_each               = toset(local.key_owners)
  secret                 = google_secret_manager_secret.tenant_key[each.key].id
  secret_data_wo         = "sk-nw-${each.key}-${ephemeral.random_password.tenant_key[each.key].result}"
  secret_data_wo_version = tostring(var.secrets_generation)
}

# The tenant identity reads its own key (preflight, notebooks); nothing else of the gateway.
resource "google_secret_manager_secret_iam_member" "identity_key" {
  for_each  = toset(var.tenants)
  project   = var.project
  secret_id = google_secret_manager_secret.tenant_key[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.tenant_users[each.key]}"
}

output "url" { value = google_cloud_run_v2_service.gateway.uri }
output "service_name" { value = google_cloud_run_v2_service.gateway.name }
output "service_account" { value = google_service_account.gateway.email }
output "master_key_secret" { value = google_secret_manager_secret.gateway["master-key"].secret_id }
output "tenant_key_secrets" { value = { for k, s in google_secret_manager_secret.tenant_key : k => s.secret_id if k != "live" } }
output "live_key_secret" { value = google_secret_manager_secret.tenant_key["live"].secret_id }
output "tenant_budget_usd" { value = var.tenant_budget_usd }
