# Prompts and retrieval. Prompt versions have no Terraform resource: the Gen AI SDK's prompt
# management (vertexai.preview.prompts, google-cloud-aiplatform) is called by
# scripts/gcp_prompts.py at apply time for every tenant, and nw/platform/gcp.py keeps the
# stage of each version in the artifacts bucket because the online store has no stages.
# Retrieval is a RAG Engine corpus per tenant, on RagManagedDb by default or on a Vertex AI
# Vector Search index per tenant when rag_backend is vector_search.
variable "project" { type = string }
variable "project_number" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "tenants" { type = list(string) }
variable "artifacts_bucket" { type = string }
variable "embedding_model" { type = string }
variable "rag_backend" { type = string }
variable "rag_managed_db_tier" { type = string }
variable "rag_unprovision_on_destroy" { type = bool }

locals {
  embedding_endpoint = "projects/${var.project_number}/locations/${var.region}/publishers/google/models/${var.embedding_model}"
  vector_search      = var.rag_backend == "vector_search"
}

resource "null_resource" "prompts" {
  for_each = toset(var.tenants)
  triggers = {
    tenant      = each.key
    environment = var.environment
    project     = var.project
    region      = var.region
    bucket      = var.artifacts_bucket
  }
  provisioner "local-exec" {
    command     = "uv run python scripts/gcp_prompts.py --project ${self.triggers.project} --region ${self.triggers.region} --bucket ${self.triggers.bucket} --environment ${self.triggers.environment} --tenant ${self.triggers.tenant}"
    working_dir = "${path.module}/../../../.."
  }
}

# Project-wide RagManagedDb tier. Left alone unless asked, because the resource is one per
# project and a second environment in the same project would fight over it. The first corpus on
# RagManagedDb provisions the project's tier (Basic: a Spanner instance of 100 processing units,
# billed every hour whether or not anything is queried, deploy/COSTS-platform.md), and deleting
# the corpora does not stop it.
resource "google_vertex_ai_rag_engine_config" "tier" {
  count   = var.rag_managed_db_tier == "" ? 0 : 1
  project = var.project
  region  = var.region
  rag_managed_db_config {
    dynamic "basic" {
      for_each = var.rag_managed_db_tier == "basic" ? [1] : []
      content {}
    }
    dynamic "scaled" {
      for_each = var.rag_managed_db_tier == "scaled" ? [1] : []
      content {}
    }
    dynamic "unprovisioned" {
      for_each = var.rag_managed_db_tier == "unprovisioned" ? [1] : []
      content {}
    }
  }
}

# Destroy sets the tier to Unprovisioned, which deletes the RagManagedDb instance and ends the
# hourly charge (updateRagEngineConfig, v1). The corpora depend on this resource, so they are
# deleted first. Turn rag_unprovision_on_destroy off when another environment in the same
# project still uses RAG Engine on RagManagedDb.
resource "null_resource" "rag_unprovision_on_destroy" {
  count = !local.vector_search && var.rag_unprovision_on_destroy ? 1 : 0
  triggers = {
    project = var.project
    region  = var.region
  }
  provisioner "local-exec" {
    when    = destroy
    command = "curl -sf -X PATCH -H \"Authorization: Bearer $(gcloud auth print-access-token)\" -H 'Content-Type: application/json' https://${self.triggers.region}-aiplatform.googleapis.com/v1/projects/${self.triggers.project}/locations/${self.triggers.region}/ragEngineConfig -d '{\"name\": \"projects/${self.triggers.project}/locations/${self.triggers.region}/ragEngineConfig\", \"ragManagedDbConfig\": {\"unprovisioned\": {}}}' && echo 'RagManagedDb set to Unprovisioned'"
  }
}

# ----- optional: Vector Search underneath -----------------------------------------------
# RAG Engine requires STREAM_UPDATE and DOT_PRODUCT or COSINE distance, a public index
# endpoint, and a deployed index; the dimension matches the embedding model (768 for
# text-embedding-005). Each deployed index bills per node hour, which is why managed is the
# default (deploy/COSTS-platform.md).

resource "google_vertex_ai_index" "policies" {
  for_each            = local.vector_search ? toset(var.tenants) : toset([])
  project             = var.project
  region              = var.region
  display_name        = "${var.environment}-${each.key}-policies"
  description         = "Policy corpus vectors of ${var.environment}-${each.key} for RAG Engine"
  index_update_method = "STREAM_UPDATE"
  labels              = merge(var.labels, { tenant = each.key, area = "retrieval" })
  metadata {
    config {
      dimensions                  = 768
      approximate_neighbors_count = 50
      distance_measure_type       = "DOT_PRODUCT_DISTANCE"
      shard_size                  = "SHARD_SIZE_SMALL"
      algorithm_config {
        tree_ah_config {}
      }
    }
  }
}

resource "google_vertex_ai_index_endpoint" "policies" {
  count                   = local.vector_search ? 1 : 0
  project                 = var.project
  region                  = var.region
  display_name            = "${var.environment}-policies"
  description             = "Public index endpoint shared by every tenant's policy index"
  public_endpoint_enabled = true
  labels                  = merge(var.labels, { area = "retrieval" })
}

resource "google_vertex_ai_index_endpoint_deployed_index" "policies" {
  for_each          = local.vector_search ? toset(var.tenants) : toset([])
  region            = var.region
  index_endpoint    = google_vertex_ai_index_endpoint.policies[0].id
  index             = google_vertex_ai_index.policies[each.key].id
  deployed_index_id = replace("${var.environment}_${each.key}_policies", "-", "_")
  display_name      = "${var.environment}-${each.key}-policies"
  automatic_resources {
    min_replica_count = 1
    max_replica_count = 1
  }
}

# ----- the corpus per tenant ----------------------------------------------------------------

resource "google_vertex_ai_rag_corpus" "policies" {
  for_each     = toset(var.tenants)
  project      = var.project
  region       = var.region
  display_name = "${var.environment}-${each.key}-policies"
  description  = "Policy corpus of ${var.environment}-${each.key}; files are imported by nw.platform.gcp.RagEngineVectorStore.upsert"

  vector_db_config {
    dynamic "rag_managed_db" {
      for_each = local.vector_search ? [] : [1]
      content {
        knn {}
      }
    }
    dynamic "vertex_vector_search" {
      for_each = local.vector_search ? [1] : []
      content {
        index          = google_vertex_ai_index.policies[each.key].id
        index_endpoint = google_vertex_ai_index_endpoint.policies[0].id
      }
    }
    rag_embedding_model_config {
      vertex_prediction_endpoint {
        endpoint = local.embedding_endpoint
      }
    }
  }
  depends_on = [google_vertex_ai_index_endpoint_deployed_index.policies, google_vertex_ai_rag_engine_config.tier, null_resource.rag_unprovision_on_destroy]
}

output "corpora" { value = { for k, c in google_vertex_ai_rag_corpus.policies : k => c.name } }
output "embedding_endpoint" { value = local.embedding_endpoint }
