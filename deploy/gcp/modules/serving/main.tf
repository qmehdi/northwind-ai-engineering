# Serving, tenant side: triage, semantic and policy on Cloud Run from the course images.
# Triage and semantic are thin clients of the registry: NW_MODEL_URI points at the artifact
# of the version the tenant deployed (nw.platform.gcp.CloudRunEndpointClient.deploy updates
# it); the policy service is the RAG inference layer over the tenant's RAG Engine corpus.
# The agent runs on Agent Engine (modules/agents). Every model call goes through the gateway.
variable "project" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "tenants" { type = list(string) }
variable "registry" { type = string }
variable "image_tag" { type = string }
variable "api_key_secret" { type = string }
variable "gateway_url" { type = string }
variable "gateway_key_secrets" { type = map(string) }
variable "artifacts_bucket" { type = string }
variable "rag_corpora" { type = map(string) }
variable "public" { type = bool }

locals {
  services = {
    triage   = { cpu = "1", memory = "2Gi", timeout = 120, env = {} }
    semantic = { cpu = "1", memory = "3Gi", timeout = 120, env = {} }
    policy   = { cpu = "1", memory = "3Gi", timeout = 120, env = { NW_RETRIEVER = "rag-engine" } }
  }
  pairs = {
    for pair in setproduct(var.tenants, keys(local.services)) :
    "${pair[0]}-${pair[1]}" => { tenant = pair[0], service = pair[1] }
  }
}

module "service" {
  for_each   = local.pairs
  source     = "../service"
  name       = "${var.environment}-${each.key}"
  account_id = "nw-${each.key}"
  project    = var.project
  region     = var.region
  image      = "${var.registry}/nw-${each.value.service}:${var.image_tag}"
  labels     = merge(var.labels, { tenant = each.value.tenant, area = "serving" })
  cpu        = local.services[each.value.service].cpu
  memory     = local.services[each.value.service].memory
  timeout    = local.services[each.value.service].timeout
  public     = var.public
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
    NW_API_KEY     = var.api_key_secret
    NW_GATEWAY_KEY = var.gateway_key_secrets[each.value.tenant]
  }
  buckets_read = [var.artifacts_bucket]
}

output "urls" { value = { for k, m in module.service : k => m.url } }
output "names" { value = { for k, m in module.service : k => m.name } }
output "service_accounts" { value = { for k, m in module.service : k => m.service_account } }
