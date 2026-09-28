# The Reference stack on GCP, layered on the Session path in the same project:
# - the resolver on Vertex AI Agent Engine, from the same container image
# - a Model Armor template screening prompt injection, jailbreak and sensitive data
# - the MCP tool server on Cloud Run, invokable only by the agent's service account
# - optionally Cloud SQL Postgres with pgvector as the managed retriever
#
#   make deploy-gcp TIER=reference     (after the session tier)

locals {
  registry       = "${var.region}-docker.pkg.dev/${var.project}/northwind"
  prefix         = var.stage == "" ? "northwind" : "northwind-${var.stage}"
  api_key_secret = "${local.prefix}-api-key" # created by the session tier
  # The Session path services the Agent Engine resolver calls as HTTP tools.
  session_services = toset(["policy", "triage", "semantic"])
}

# The session tier is applied first in the same project; its services are read here so
# the resolver on Agent Engine calls Projects 1 to 3 over HTTP with the cohort API key.
data "google_cloud_run_v2_service" "session" {
  for_each = local.session_services
  project  = var.project
  location = var.region
  name     = "${local.prefix}-${each.key}"
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
  template_id = "${local.prefix}-support"

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
  account_id   = "${local.prefix}-agent-engine"
  display_name = "Northwind resolver on Agent Engine${var.stage == "" ? "" : " (${var.stage})"}"
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

resource "google_project_iam_member" "agent_engine_trace" {
  project = var.project
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.agent_engine.email}"
}

# The runtime reads the cohort API key from Secret Manager so the resolver starts with
# auth on and sends the key to the Session path tools.
resource "google_secret_manager_secret_iam_member" "agent_engine_api_key" {
  project   = var.project
  secret_id = local.api_key_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_engine.email}"
}

module "mcp" {
  source         = "../modules/service"
  name           = "mcp"
  project        = var.project
  region         = var.region
  image          = "${local.registry}/nw-mcp:${var.image_tag}"
  cpu            = "2"
  memory         = "4Gi"
  invoke_models  = false
  timeout        = 300
  env            = { NW_MCP_HOST = "0.0.0.0", NW_MCP_PORT = "8000" }
  public         = false
  api_key_secret = local.api_key_secret
  stage          = var.stage
}

resource "google_cloud_run_v2_service_iam_member" "mcp_invoker" {
  project  = var.project
  location = var.region
  name     = module.mcp.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.agent_engine.email}"
}

# ----- the resolver on Agent Engine, from the course image ---------------------------
#
# The image is the same nw-agent image the Session path runs; NW_APP switches uvicorn to
# nw.agent.agentcore:app, which serves the Agent Engine runtime contract
# (POST /api/reasoning_engine and /api/stream_reasoning_engine with
# {"class_method": "route", "input": {...}}). class_methods must be declared by hand for a
# container deployment or the SDK cannot discover the method and every call fails.

resource "google_vertex_ai_reasoning_engine" "resolver" {
  project      = var.project
  region       = var.region
  display_name = "${local.prefix}-resolver"
  description  = "Northwind support resolution agent"

  spec {
    agent_framework = "custom"
    service_account = google_service_account.agent_engine.email

    class_methods = jsonencode([
      {
        name        = "route"
        api_mode    = ""
        description = "Route one support ticket: triage, retrieval and a proposed resolution"
        parameters = {
          type     = "object"
          required = ["ticket_id", "account_id", "subject", "task"]
          properties = {
            ticket_id  = { type = "string" }
            account_id = { type = "string" }
            subject    = { type = "string" }
            task       = { type = "string", description = "The ticket body" }
          }
        }
      }
    ])

    container_spec {
      image_uri = "${local.registry}/nw-agent:${var.image_tag}"
      port      = 8000
    }

    deployment_spec {
      min_instances         = 0
      max_instances         = 2
      container_concurrency = 5 # 2 x cpu + 1, the provider's recommendation
      resource_limits       = { cpu = "2", memory = "4Gi" }

      env {
        name  = "NW_APP"
        value = "nw.agent.agentcore:app"
      }
      env {
        name  = "PORT"
        value = "8000"
      }
      env {
        name  = "NW_TRACK"
        value = "gcp"
      }
      env {
        name  = "NW_GCP_PROJECT"
        value = var.project
      }
      env {
        name  = "NW_GCP_REGION"
        value = "global"
      }
      env {
        name  = "NW_LOG_FORMAT"
        value = "json"
      }
      env {
        name  = "NW_TRACE_EXPORT"
        value = "cloudtrace"
      }
      env {
        name  = "OTEL_SERVICE_NAME"
        value = "${local.prefix}-resolver-agent-engine"
      }
      env {
        name  = "NW_STAGE"
        value = var.stage
      }
      env {
        name  = "NW_AGENT_ROLE"
        value = "resolver"
      }
      env {
        name  = "NW_SPEND_CAP_USD"
        value = "25"
      }
      env {
        name  = "NW_MODEL_ARMOR_TEMPLATE"
        value = google_model_armor_template.support.name
      }
      env {
        name  = "NW_TOOL_BACKEND"
        value = "http"
      }
      env {
        name  = "NW_POLICY_URL"
        value = data.google_cloud_run_v2_service.session["policy"].uri
      }
      env {
        name  = "NW_TRIAGE_URL"
        value = data.google_cloud_run_v2_service.session["triage"].uri
      }
      env {
        name  = "NW_SEMANTIC_URL"
        value = data.google_cloud_run_v2_service.session["semantic"].uri
      }

      secret_env {
        name = "NW_API_KEY"
        secret_ref {
          secret  = local.api_key_secret
          version = "latest"
        }
      }
    }
  }
  depends_on = [
    google_project_service.apis,
    google_project_iam_member.agent_engine_models,
    google_project_iam_member.agent_engine_armor,
    google_project_iam_member.agent_engine_trace,
    google_secret_manager_secret_iam_member.agent_engine_api_key,
  ]
}

# ----- optional: pgvector on Cloud SQL ---------------------------------------------

resource "google_sql_database_instance" "pgvector" {
  count               = var.enable_pgvector ? 1 : 0
  project             = var.project
  name                = "${local.prefix}-pgvector"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = false
  settings {
    tier              = "db-custom-1-3840"
    edition           = "ENTERPRISE"
    availability_type = "ZONAL"
    disk_size         = 10
    # Public IP with no authorized networks and SSL required: only the Cloud SQL Auth Proxy
    # or a client holding the server certificate can connect. Private IP needs a VPC and a
    # Service Networking peering, which the course does not create.
    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }
  }
  depends_on = [google_project_service.apis]
}

output "agent_engine" { value = google_vertex_ai_reasoning_engine.resolver.name }
output "model_armor_template" { value = google_model_armor_template.support.name }
output "mcp_url" { value = module.mcp.url }
