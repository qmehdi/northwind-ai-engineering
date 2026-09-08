# The Reference stack on GCP, layered on the Session path in the same project:
# - the resolver on Vertex AI Agent Engine, from the same container image
# - a Model Armor template screening prompt injection, jailbreak and sensitive data
# - the MCP tool server on Cloud Run, invokable only by the agent's service account
# - optionally Cloud SQL Postgres with pgvector as the managed retriever
#
#   make deploy-gcp TIER=reference     (after the session tier)

locals {
  registry = "${var.region}-docker.pkg.dev/${var.project}/northwind"
}

resource "google_project_service" "apis" {
  for_each           = toset(["aiplatform.googleapis.com", "modelarmor.googleapis.com", "sqladmin.googleapis.com"])
  project            = var.project
  service            = each.value
  disable_on_destroy = false
}

# ----- Model Armor ---------------------------------------------------------------

resource "google_model_armor_template" "support" {
  project     = var.project
  location    = var.region
  template_id = "northwind-support"

  filter_config {
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }
    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }
    sdp_settings {
      basic_config {
        filter_enforcement = "ENABLED"
      }
    }
    rai_settings {
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "HIGH"
      }
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = "HIGH"
      }
    }
  }

  template_metadata {
    enforcement_type                   = "INSPECT_AND_BLOCK"
    log_sanitize_operations            = true
    ignore_partial_invocation_failures = true
    custom_prompt_safety_error_code    = 400
    custom_prompt_safety_error_message = "This request was blocked by Northwind's safety policy."
  }
  depends_on = [google_project_service.apis]
}

# ----- the MCP tool server on Cloud Run, private ----------------------------------

resource "google_service_account" "agent_engine" {
  project      = var.project
  account_id   = "northwind-agent-engine"
  display_name = "Northwind resolver on Agent Engine"
}

resource "google_project_iam_member" "agent_engine_models" {
  project = var.project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.agent_engine.email}"
}

resource "google_project_iam_member" "agent_engine_armor" {
  project = var.project
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${google_service_account.agent_engine.email}"
}

module "mcp" {
  source        = "../modules/service"
  name          = "mcp"
  project       = var.project
  region        = var.region
  image         = "${local.registry}/nw-mcp:${var.image_tag}"
  cpu           = "2"
  memory        = "4Gi"
  invoke_models = false
  env           = { NW_MCP_HOST = "0.0.0.0", NW_MCP_PORT = "8000" }
  public        = false
}

resource "google_cloud_run_v2_service_iam_member" "mcp_invoker" {
  project  = var.project
  location = var.region
  name     = module.mcp.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.agent_engine.email}"
}

# ----- the resolver on Agent Engine, from the course image ---------------------------

resource "google_vertex_ai_reasoning_engine" "resolver" {
  project      = var.project
  region       = var.region
  display_name = "northwind-resolver"
  description  = "Northwind support resolution agent"

  spec {
    agent_framework = "custom"
    service_account = google_service_account.agent_engine.email

    container_spec {
      image_uri = "${local.registry}/nw-agent:${var.image_tag}"
      port      = 8000
    }

    deployment_spec {
      min_instances = 0
      max_instances = 2
      env {
        name  = "NW_TRACK"
        value = "gcp"
      }
      env {
        name  = "NW_GCP_PROJECT"
        value = var.project
      }
      env {
        name  = "NW_AGENT_ROLE"
        value = "resolver"
      }
      env {
        name  = "NW_MODEL_ARMOR_TEMPLATE"
        value = google_model_armor_template.support.name
      }
      env {
        name  = "NW_MCP_URL"
        value = "${module.mcp.url}/mcp"
      }
    }
  }
  depends_on = [google_project_service.apis, google_project_iam_member.agent_engine_models]
}

# ----- optional: pgvector on Cloud SQL ---------------------------------------------

resource "google_sql_database_instance" "pgvector" {
  count               = var.enable_pgvector ? 1 : 0
  project             = var.project
  name                = "northwind-pgvector"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = false
  settings {
    tier              = "db-custom-1-3840"
    edition           = "ENTERPRISE"
    availability_type = "ZONAL"
    disk_size         = 10
    ip_configuration {
      ipv4_enabled = false
    }
  }
}

output "agent_engine" { value = google_vertex_ai_reasoning_engine.resolver.name }
output "model_armor_template" { value = google_model_armor_template.support.name }
output "mcp_url" { value = module.mcp.url }
