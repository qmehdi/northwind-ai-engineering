# Agents: the resolver on Agent Engine per tenant from the nw-agent image (the contract in
# nw/agent/agentcore.py: POST /api/reasoning_engine with {"class_method": "route", "input":
# {...}}), one Model Armor template for the environment, a service account per tenant, and
# the agent registry document.
#
# Registry: Google's Agent Registry (agentregistry.googleapis.com, surfaced through IAP for
# agents and Agent Gateway) exists in preview, but the google provider 8.4.0 carries only its
# IAM bindings (google_iap_agent_registry_*_iam_*), not the registry, agent or endpoint
# resources. The course therefore keeps its registry as a document, agents/agents.json in the
# artifacts bucket, seeded here and updated by nw.platform.gcp.AgentEngineRuntime.register.
variable "project" { type = string }
variable "project_number" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "tenants" { type = list(string) }
variable "registry" { type = string }
variable "image_tag" { type = string }
variable "api_key_secret" { type = string }
variable "gateway_url" { type = string }
variable "gateway_key_secrets" { type = map(string) }
variable "service_urls" { type = map(string) }
variable "service_names" { type = map(string) }
variable "artifacts_bucket" { type = string }

locals {
  reasoning_engine_agent = "service-${var.project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
}

# ----- Model Armor, one template per environment ----------------------------------------------

resource "google_model_armor_template" "support" {
  project     = var.project
  location    = var.region
  template_id = "${var.environment}-support"
  labels      = merge(var.labels, { area = "agents" })

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
}

# ----- identity per tenant ------------------------------------------------------------------

resource "google_service_account" "agent" {
  for_each     = toset(var.tenants)
  project      = var.project
  account_id   = "nw-${each.key}-agent"
  display_name = "${var.environment}-${each.key} resolver on Agent Engine"
}

locals {
  agent_roles = ["roles/aiplatform.user", "roles/modelarmor.user", "roles/cloudtrace.agent", "roles/logging.logWriter", "roles/monitoring.metricWriter"]
  agent_role_pairs = {
    for pair in setproduct(var.tenants, local.agent_roles) : "${pair[0]}:${pair[1]}" => { tenant = pair[0], role = pair[1] }
  }
  tool_pairs = {
    for pair in setproduct(var.tenants, ["triage", "semantic", "policy"]) : "${pair[0]}-${pair[1]}" => { tenant = pair[0], service = pair[1] }
  }
}

resource "google_project_iam_member" "agent" {
  for_each = local.agent_role_pairs
  project  = var.project
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.agent[each.value.tenant].email}"
}

# The agent calls its tenant's three services as HTTP tools with the cohort API key; it is
# also an invoker so the services can be made private later without touching the agent.
resource "google_cloud_run_v2_service_iam_member" "tools" {
  for_each = local.tool_pairs
  project  = var.project
  location = var.region
  name     = var.service_names[each.key]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.agent[each.value.tenant].email}"
}

resource "google_secret_manager_secret_iam_member" "api_key" {
  for_each  = toset(var.tenants)
  project   = var.project
  secret_id = var.api_key_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent[each.key].email}"
}

resource "google_secret_manager_secret_iam_member" "gateway_key" {
  for_each  = toset(var.tenants)
  project   = var.project
  secret_id = var.gateway_key_secrets[each.key]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent[each.key].email}"
}

resource "google_storage_bucket_iam_member" "agent_artifacts" {
  for_each = toset(var.tenants)
  bucket   = var.artifacts_bucket
  role     = "roles/storage.objectAdmin"
  member   = "serviceAccount:${google_service_account.agent[each.key].email}"
}

# The Reasoning Engine service agent pulls the container image and the secrets named in
# secret_env when it builds the deployment.
resource "google_project_iam_member" "reasoning_engine_images" {
  project = var.project
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${local.reasoning_engine_agent}"
}

resource "google_project_iam_member" "reasoning_engine_secrets" {
  project = var.project
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${local.reasoning_engine_agent}"
}

# ----- the resolver per tenant --------------------------------------------------------------
# class_methods must be declared by hand for a container deployment or the SDK cannot discover
# the method and every query fails. Environment (NW_*) matches the Cloud Run agent service so
# the same image behaves the same on both runtimes.

resource "google_vertex_ai_reasoning_engine" "resolver" {
  for_each     = toset(var.tenants)
  project      = var.project
  region       = var.region
  display_name = "${var.environment}-${each.key}-agent"
  description  = "Northwind support resolution agent of tenant ${each.key}"
  labels       = merge(var.labels, { tenant = each.key, area = "agents" })

  spec {
    agent_framework = "custom"
    service_account = google_service_account.agent[each.key].email

    class_methods = jsonencode([
      {
        name        = "route"
        api_mode    = ""
        description = "Route one support ticket: triage, retrieval and a proposed resolution"
        parameters = {
          type     = "object"
          required = ["task"]
          properties = {
            ticket_id  = { type = "string" }
            account_id = { type = "string" }
            subject    = { type = "string" }
            task       = { type = "string", description = "The ticket body" }
            session_id = { type = "string" }
          }
        }
      }
    ])

    container_spec {
      image_uri = "${var.registry}/nw-agent:${var.image_tag}"
      port      = 8000
    }

    deployment_spec {
      min_instances         = 0
      max_instances         = 2
      container_concurrency = 5
      resource_limits       = { cpu = "2", memory = "4Gi" }

      dynamic "env" {
        for_each = {
          NW_APP                  = "nw.agent.agentcore:app"
          PORT                    = "8000"
          NW_TRACK                = "gcp"
          NW_GCP_PROJECT          = var.project
          NW_GCP_REGION           = "global"
          NW_GCP_RUN_REGION       = var.region
          NW_LOG_FORMAT           = "json"
          NW_TRACE_EXPORT         = "cloudtrace"
          OTEL_SERVICE_NAME       = "${var.environment}-${each.key}-agent"
          NW_TENANT               = each.key
          NW_ENVIRONMENT          = var.environment
          NW_AGENT_ROLE           = "resolver"
          NW_SPEND_CAP_USD        = "25"
          NW_GATEWAY_URL          = var.gateway_url
          NW_MODEL_ARMOR_TEMPLATE = google_model_armor_template.support.name
          NW_TOOL_BACKEND         = "http"
          NW_TRIAGE_URL           = var.service_urls["${each.key}-triage"]
          NW_SEMANTIC_URL         = var.service_urls["${each.key}-semantic"]
          NW_POLICY_URL           = var.service_urls["${each.key}-policy"]
          NW_AGENT_REGISTRY       = "gs://${var.artifacts_bucket}/agents/agents.json"
        }
        content {
          name  = env.key
          value = env.value
        }
      }

      secret_env {
        name = "NW_API_KEY"
        secret_ref {
          secret  = var.api_key_secret
          version = "latest"
        }
      }
      secret_env {
        name = "NW_GATEWAY_KEY"
        secret_ref {
          secret  = var.gateway_key_secrets[each.key]
          version = "latest"
        }
      }
    }
  }

  # A tenant's deploy through the platform client updates the image and env in place; the
  # next apply must not roll that back. Replace the engine to change these from Terraform.
  lifecycle {
    ignore_changes = [spec[0].container_spec, spec[0].deployment_spec[0].env]
  }

  depends_on = [
    google_project_iam_member.agent,
    google_secret_manager_secret_iam_member.api_key,
    google_secret_manager_secret_iam_member.gateway_key,
    google_project_iam_member.reasoning_engine_images,
    google_project_iam_member.reasoning_engine_secrets,
  ]
}

# ----- the agent registry document ------------------------------------------------------------

resource "google_storage_bucket_object" "registry" {
  bucket       = var.artifacts_bucket
  name         = "agents/agents.json"
  content_type = "application/json"
  content = jsonencode({
    environment = var.environment
    note        = "Agent registry of the course: every agent that is in production is in this document. Updated by nw.platform.gcp.AgentEngineRuntime.register."
    agents = [
      for t in var.tenants : {
        id              = "${var.environment}-${t}-agent"
        tenant          = t
        runtime         = "agent-engine"
        resource        = google_vertex_ai_reasoning_engine.resolver[t].name
        service_account = google_service_account.agent[t].email
        image           = "${var.registry}/nw-agent:${var.image_tag}"
        status          = "registered"
        use_case        = "support-resolution"
      }
    ]
  })
  # The runtime rewrites the document; Terraform seeds it once and leaves it alone.
  lifecycle {
    ignore_changes = [content, detect_md5hash]
  }
}

output "engines" { value = { for k, e in google_vertex_ai_reasoning_engine.resolver : k => e.name } }
output "service_accounts" { value = { for k, sa in google_service_account.agent : k => sa.email } }
output "model_armor_template" { value = google_model_armor_template.support.name }
output "registry_uri" { value = "gs://${var.artifacts_bucket}/${google_storage_bucket_object.registry.name}" }
