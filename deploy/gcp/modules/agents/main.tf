# Agents: the resolver on Agent Engine per tenant from the nw-agent image (the contract in
# nw/agent/agentcore.py: POST /api/reasoning_engine with {"class_method": "route", "input":
# {...}}), one Model Armor template for the environment, a service account per tenant, and
# the agent registry document.
#
# Registry: Google's Agent Registry (agentregistry.googleapis.com, surfaced through IAP for
# agents and Agent Gateway) exists in preview, but the google provider 8.4.0 carries only its
# IAM bindings (google_iap_agent_registry_*_iam_*), not the registry, agent or endpoint
# resources. The course therefore keeps its registry as a document, agents/agents.json in the
# artifacts bucket, seeded here. The document is platform-owned: tenants read `agents/` and write
# their own card under `<environment>-<tenant>/agents/`; the platform merges the cards.
#
# Secrets: the engine gets no `secret_env`. The Reasoning Engine service agent would
# read every secret named there, one service agent for the whole project, so it holds no Secret
# Manager role at all (and the deny policy in modules/guardrails denies it every secret). The
# engine's own service account reads its tenant's API key and gateway key at start through
# NW_API_KEY_SECRET_NAME and NW_GATEWAY_KEY_SECRET_NAME (nw/auth.py, nw/serving/gateway.py), and
# has access to those two secrets only. A tenant cannot create or delete engines (the custom
# tenant role lacks reasoningEngines.create|delete); it may update and query its own engine,
# granted on that engine, and the engine keeps running as the tenant's agent identity because
# actAs is granted on that account only.
variable "project" { type = string }
variable "project_number" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "tenants" { type = list(string) }
variable "registry" { type = string }
variable "image_tag" { type = string }
variable "api_key_secrets" {
  type        = map(string)
  description = "Tenant to the secret id of its service API key (modules/identity)"
}
variable "gateway_url" { type = string }
variable "gateway_key_secrets" { type = map(string) }
variable "service_urls" { type = map(string) }
variable "service_names" { type = map(string) }
variable "artifacts_bucket" { type = string }
variable "folders" { type = map(string) }
variable "roles" { type = map(string) }
variable "tenant_users" { type = map(string) }
variable "mcp_urls" {
  type        = map(string)
  description = "Tenant to the URL of its private MCP service (modules/serving)"
}
variable "mcp_names" { type = map(string) }
variable "images_repository" {
  type        = string
  description = "Artifact Registry repository id of the course images"
}

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
  # Static keys: the custom role's id is known only after apply. No roles/aiplatform.user: the
  # runtime role has sessions and Memory Bank, and models are reached through the gateway.
  agent_roles = {
    runtime = var.roles["agent_runtime"]
    armor   = "roles/modelarmor.user"
    trace   = "roles/cloudtrace.agent"
    logs    = "roles/logging.logWriter"
    metrics = "roles/monitoring.metricWriter"
  }
  agent_role_pairs = {
    for pair in setproduct(var.tenants, keys(local.agent_roles)) : "${pair[0]}:${pair[1]}" => { tenant = pair[0], role = local.agent_roles[pair[1]] }
  }
  secret_name = "projects/${var.project}/secrets"
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

# The agent calls its tenant's three services as HTTP tools with its tenant's API key; it is
# also an invoker so the services can be made private without touching the agent.
resource "google_cloud_run_v2_service_iam_member" "tools" {
  for_each = local.tool_pairs
  project  = var.project
  location = var.region
  name     = var.service_names[each.key]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.agent[each.value.tenant].email}"
}

# The private MCP service of the tenant: the agent identity is its only invoker.
resource "google_cloud_run_v2_service_iam_member" "mcp" {
  for_each = toset(var.tenants)
  project  = var.project
  location = var.region
  name     = var.mcp_names[each.key]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.agent[each.key].email}"
}

resource "google_secret_manager_secret_iam_member" "api_key" {
  for_each  = toset(var.tenants)
  project   = var.project
  secret_id = var.api_key_secrets[each.key]
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

# The tenant's folder read only; write only where the ops store puts a runtime's state
# (NW_OPS_STORE): `trajectories/` (every run with its proposed actions) and `feedback/`. Not
# `approvals/`: claim markers, approval records and the escalation queue are written by the
# approver (the tenant identity, which holds its whole folder), so an agent that got past the
# loop's gate still cannot queue an escalation (ADR 0005). The registry document read only.
resource "google_storage_managed_folder_iam_member" "agent_own" {
  for_each       = toset(var.tenants)
  bucket         = var.artifacts_bucket
  managed_folder = var.folders["artifacts:${each.key}"]
  role           = "roles/storage.objectViewer"
  member         = "serviceAccount:${google_service_account.agent[each.key].email}"
}

resource "google_storage_managed_folder_iam_member" "agent_ops" {
  for_each       = { for pair in setproduct(var.tenants, ["trajectories", "feedback"]) : "${pair[0]}:${pair[1]}" => { tenant = pair[0], kind = pair[1] } }
  bucket         = var.artifacts_bucket
  managed_folder = var.folders["ops:${each.value.tenant}:${each.value.kind}"]
  role           = "roles/storage.objectUser"
  member         = "serviceAccount:${google_service_account.agent[each.value.tenant].email}"
}

resource "google_storage_managed_folder_iam_member" "agent_registry" {
  for_each       = toset(var.tenants)
  bucket         = var.artifacts_bucket
  managed_folder = var.folders["artifacts:agents"]
  role           = "roles/storage.objectViewer"
  member         = "serviceAccount:${google_service_account.agent[each.key].email}"
}

# The Reasoning Engine service agent pulls the container image from the course repository
# only. It holds no Secret Manager role: the engines carry no secret_env.
resource "google_artifact_registry_repository_iam_member" "reasoning_engine_images" {
  project    = var.project
  location   = var.region
  repository = var.images_repository
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${local.reasoning_engine_agent}"
}

# The learner: update and query its own engine, act as its own agent identity.
resource "google_vertex_ai_reasoning_engine_iam_member" "tenant" {
  for_each         = toset(var.tenants)
  project          = var.project
  region           = var.region
  reasoning_engine = google_vertex_ai_reasoning_engine.resolver[each.key].name
  role             = var.roles["tenant_engine"]
  member           = "serviceAccount:${var.tenant_users[each.key]}"
}

resource "google_service_account_iam_member" "tenant_acts_as" {
  for_each           = toset(var.tenants)
  service_account_id = google_service_account.agent[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.tenant_users[each.key]}"
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
          # The model-backed tools through the tenant's private MCP service with an ID token;
          # the customer tools and escalate stay in process (nw/agent/mcp_client.py). The
          # service URLs and NW_TOOL_AUTH serve NW_TOOL_BACKEND=http, the documented fallback.
          NW_TOOL_BACKEND   = "mcp"
          NW_MCP_URL        = "${var.mcp_urls[each.key]}/mcp"
          NW_MCP_AUTH       = "google-id-token"
          NW_TOOL_AUTH      = "google-id-token"
          NW_TRIAGE_URL     = var.service_urls["${each.key}-triage"]
          NW_SEMANTIC_URL   = var.service_urls["${each.key}-semantic"]
          NW_POLICY_URL     = var.service_urls["${each.key}-policy"]
          NW_AGENT_REGISTRY = "gs://${var.artifacts_bucket}/agents/agents.json"
          # Trajectories in the tenant's folder, not on the engine's disk (nw/agent/opstore.py).
          NW_OPS_STORE       = "gs://${var.artifacts_bucket}"
          NW_REDACT_DETECTOR = "heuristic"
          # Agent Engine authorises every query with IAM (reasoningEngines.query on this engine)
          # and forwards no custom key: the contract routes are the platform's to check.
          NW_RUNTIME_AUTH = "platform"
          # Secrets by reference, read by the engine's own service account (see the header).
          NW_API_KEY_SECRET_NAME     = "${local.secret_name}/${var.api_key_secrets[each.key]}/versions/latest"
          NW_GATEWAY_KEY_SECRET_NAME = "${local.secret_name}/${var.gateway_key_secrets[each.key]}/versions/latest"
        }
        content {
          name  = env.key
          value = env.value
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
    google_artifact_registry_repository_iam_member.reasoning_engine_images,
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
  # The platform merges the tenants' cards into the document; Terraform seeds it once.
  lifecycle {
    ignore_changes = [content, detect_md5hash]
  }
}

output "engines" { value = { for k, e in google_vertex_ai_reasoning_engine.resolver : k => e.name } }
output "service_accounts" { value = { for k, sa in google_service_account.agent : k => sa.email } }
output "model_armor_template" { value = google_model_armor_template.support.name }
output "registry_uri" { value = "gs://${var.artifacts_bucket}/${google_storage_bucket_object.registry.name}" }
