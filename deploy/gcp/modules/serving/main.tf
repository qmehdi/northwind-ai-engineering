# Serving, tenant side: triage, semantic and policy on Cloud Run from the course images, and the
# tenant's MCP server. Triage and semantic are thin clients of the registry: NW_MODEL_URI points
# at the artifact of the version the tenant deployed (nw.platform.gcp.CloudRunEndpointClient.deploy
# updates it); the policy service is the RAG inference layer over the tenant's RAG Engine corpus.
# The agent runs on Agent Engine (modules/agents). Every model call goes through the gateway.
#
# Each tenant's services take that tenant's API key only (`<environment>-<tenant>-api-key`), so a
# learner cannot call, or spend the gateway budget of, another learner's services. A service
# reads its model artifacts from its tenant's managed folder, never from the whole bucket. The
# tenant identity may update, invoke and act as its own services and nobody else's.
#
# MCP: `<environment>-<tenant>-mcp` serves the tool registry (nw/agent/mcp_server.py, streamable
# HTTP at /mcp, stateless) over the tenant's three services. It is private: no allUsers, and
# the tenant's agent identity is its only invoker (modules/agents), so a caller needs a Google ID
# token for that account. The tenant identity may invoke it too, for the guide's inspection step.
variable "project" { type = string }
variable "project_number" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "tenants" { type = list(string) }
variable "registry" { type = string }
variable "image_tag" { type = string }
variable "api_key_secrets" { type = map(string) }
variable "gateway_url" { type = string }
variable "gateway_key_secrets" { type = map(string) }
variable "artifacts_bucket" { type = string }
variable "folders" { type = map(string) }
variable "roles" { type = map(string) }
variable "tenant_users" { type = map(string) }
variable "rag_corpora" { type = map(string) }
variable "public" { type = bool }

locals {
  services = {
    triage   = { cpu = "1", memory = "2Gi", timeout = 120, env = {}, roles = {} }
    semantic = { cpu = "1", memory = "3Gi", timeout = 120, env = {}, roles = {} }
    policy = {
      cpu     = "1"
      memory  = "3Gi"
      timeout = 120
      # /feedback verdicts in the tenant's feedback/ folder (nw/agent/opstore.py); names are
      # redacted before a question or a note is kept.
      env   = { NW_RETRIEVER = "rag-engine", NW_OPS_STORE = "gs://${var.artifacts_bucket}", NW_REDACT_DETECTOR = "heuristic" }
      roles = { rag = var.roles["rag_reader"] }
    }
  }
  # The policy service writes feedback only; see modules/data for the ops folders.
  ops_writes = { policy = ["feedback"] }
  pairs = {
    for pair in setproduct(var.tenants, keys(local.services)) :
    "${pair[0]}-${pair[1]}" => { tenant = pair[0], service = pair[1] }
  }
  # Deterministic Cloud Run URL: https://<service>-<project number>.<region>.run.app
  run_host = { for t in var.tenants : t => "${var.environment}-${t}-mcp-${var.project_number}.${var.region}.run.app" }
}

module "service" {
  for_each      = local.pairs
  source        = "../service"
  name          = "${var.environment}-${each.key}"
  account_id    = "nw-${each.key}"
  project       = var.project
  region        = var.region
  image         = "${var.registry}/nw-${each.value.service}:${var.image_tag}"
  labels        = merge(var.labels, { tenant = each.value.tenant, area = "serving" })
  cpu           = local.services[each.value.service].cpu
  memory        = local.services[each.value.service].memory
  timeout       = local.services[each.value.service].timeout
  public        = var.public
  project_roles = local.services[each.value.service].roles
  operators     = ["serviceAccount:${var.tenant_users[each.value.tenant]}"]
  env = merge({
    NW_TENANT      = each.value.tenant
    NW_ENVIRONMENT = var.environment
    NW_GATEWAY_URL = var.gateway_url
    # The artifact the service loads at start; the first apply points at the image's baked
    # artifact (empty means the image default) and a deploy through the registry rewrites it.
    NW_MODEL_URI      = ""
    NW_RAG_CORPUS     = var.rag_corpora[each.value.tenant]
    NW_GCP_RUN_REGION = var.region
  }, local.services[each.value.service].env)
  secret_env = {
    NW_API_KEY     = var.api_key_secrets[each.value.tenant]
    NW_GATEWAY_KEY = var.gateway_key_secrets[each.value.tenant]
  }
  folders_read = {
    own = { bucket = var.artifacts_bucket, folder = var.folders["artifacts:${each.value.tenant}"] }
  }
  folders_write = {
    for kind in lookup(local.ops_writes, each.value.service, []) :
    kind => { bucket = var.artifacts_bucket, folder = var.folders["ops:${each.value.tenant}:${kind}"] }
  }
}

module "mcp" {
  for_each      = toset(var.tenants)
  source        = "../service"
  name          = "${var.environment}-${each.key}-mcp"
  account_id    = "nw-${each.key}-mcp"
  project       = var.project
  region        = var.region
  image         = "${var.registry}/nw-mcp:${var.image_tag}"
  labels        = merge(var.labels, { tenant = each.key, area = "serving" })
  cpu           = "1"
  memory        = "3Gi"
  timeout       = 300
  public        = false
  probe_path    = ""
  liveness_path = ""
  invokers      = ["serviceAccount:${var.tenant_users[each.key]}"]
  env = {
    NW_APP               = "mcp"
    NW_TENANT            = each.key
    NW_ENVIRONMENT       = var.environment
    NW_MCP_TRANSPORT     = "streamable-http"
    NW_MCP_HOST          = "0.0.0.0"
    NW_MCP_PORT          = "8000"
    NW_MCP_STATELESS     = "1"
    NW_MCP_ALLOWED_HOSTS = "${local.run_host[each.key]},${local.run_host[each.key]}:*"
    NW_TOOL_BACKEND      = "http"
    # The three services may be private (public_services = false): an ID token per call.
    NW_TOOL_AUTH       = "google-id-token"
    NW_REDACT_DETECTOR = "heuristic"
    NW_TRIAGE_URL      = module.service["${each.key}-triage"].url
    NW_SEMANTIC_URL    = module.service["${each.key}-semantic"].url
    NW_POLICY_URL      = module.service["${each.key}-policy"].url
    NW_GATEWAY_URL     = var.gateway_url
  }
  secret_env = {
    NW_API_KEY     = var.api_key_secrets[each.key]
    NW_GATEWAY_KEY = var.gateway_key_secrets[each.key]
  }
}

# The MCP server calls the tenant's three services as tools; an invoker so the services can be
# private (public_services = false) without touching the MCP server.
resource "google_cloud_run_v2_service_iam_member" "mcp_tools" {
  for_each = local.pairs
  project  = var.project
  location = var.region
  name     = module.service[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${module.mcp[each.value.tenant].service_account}"
}

output "urls" { value = { for k, m in module.service : k => m.url } }
output "names" { value = { for k, m in module.service : k => m.name } }
output "service_accounts" { value = { for k, m in module.service : k => m.service_account } }
output "mcp_urls" { value = { for k, m in module.mcp : k => "https://${local.run_host[k]}" } }
output "mcp_names" { value = { for k, m in module.mcp : k => m.name } }
output "mcp_service_accounts" { value = { for k, m in module.mcp : k => m.service_account } }
