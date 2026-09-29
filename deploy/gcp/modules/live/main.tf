# The live target of the platform: one Vertex endpoint per registered model name with a
# traffic split (the promotion drill deploys the approved version at canary_percent and moves
# the rest after the check), Model Monitoring on each, and the identity the live Cloud Run
# services run as (Cloud Deploy creates those services from deploy/gcp/platform/delivery).
variable "project" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "models" { type = list(string) }
variable "endpoint_id_base" { type = number }
variable "monitoring" { type = bool }
variable "artifacts_bucket" { type = string }
variable "data_bucket" { type = string }
variable "api_key_secret" { type = string }

# Endpoint ids are numeric (the provider's `name`), so the base plus the model's position
# gives a stable id and the display name carries the environment.
resource "google_vertex_ai_endpoint" "live" {
  for_each     = { for i, m in var.models : m => var.endpoint_id_base + i }
  project      = var.project
  location     = var.region
  name         = tostring(each.value)
  display_name = "${var.environment}-live-${each.key}"
  description  = "Promoted ${each.key} model of ${var.environment}; deployed versions and the traffic split are set by the promotion drill, not by Terraform"
  labels       = merge(var.labels, { area = "live", model = each.key })

  # Request and response logging into BigQuery is what Model Monitoring reads for skew and
  # drift on the served traffic; a 10 percent sample keeps the table small.
  predict_request_response_logging_config {
    enabled       = true
    sampling_rate = 0.1
    bigquery_destination {
      output_uri = "bq://${var.project}.${replace(var.environment, "-", "_")}_platform.live_${each.key}_requests"
    }
  }
}

# Model Monitoring v2 has no Terraform resource (checked against google 8.4.0 and google-beta
# 8.4.0 schemas), so the monitor and its weekly schedule are created by
# scripts/gcp_model_monitor.py through the SDK (vertexai.resources.preview.ml_monitoring) and
# removed on destroy. The script waits for a model version tagged `live` and is a no-op until
# the first promotion, so a fresh apply does not fail.
resource "null_resource" "model_monitor" {
  for_each = var.monitoring ? google_vertex_ai_endpoint.live : {}
  triggers = {
    endpoint = each.value.name
    project  = var.project
    region   = var.region
    model    = "${var.environment}-live-${each.key}"
    training = "gs://${var.data_bucket}/tickets/tickets.jsonl"
    output   = "gs://${var.artifacts_bucket}/monitoring/${each.key}"
  }
  provisioner "local-exec" {
    command     = "uv run python scripts/gcp_model_monitor.py create --project ${self.triggers.project} --region ${self.triggers.region} --endpoint ${self.triggers.endpoint} --model-display-name ${self.triggers.model} --training-uri ${self.triggers.training} --output-uri ${self.triggers.output}"
    working_dir = "${path.module}/../../../.."
  }
  provisioner "local-exec" {
    when        = destroy
    command     = "uv run python scripts/gcp_model_monitor.py delete --project ${self.triggers.project} --region ${self.triggers.region} --model-display-name ${self.triggers.model}"
    working_dir = "${path.module}/../../../.."
  }
}

# The identity of the live Cloud Run services. Cloud Deploy's manifest names it; the roles
# match a tenant service's.
resource "google_service_account" "live" {
  project      = var.project
  account_id   = "${var.environment}-live"
  display_name = "${var.environment} live services"
}

resource "google_project_iam_member" "live" {
  for_each = toset(["roles/cloudtrace.agent", "roles/monitoring.metricWriter", "roles/logging.logWriter", "roles/aiplatform.user"])
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.live.email}"
}

resource "google_storage_bucket_iam_member" "live_artifacts" {
  bucket = var.artifacts_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.live.email}"
}

resource "google_secret_manager_secret_iam_member" "live_api_key" {
  project   = var.project
  secret_id = var.api_key_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.live.email}"
}

output "endpoint_ids" { value = { for k, e in google_vertex_ai_endpoint.live : k => e.name } }
output "endpoint_names" { value = { for k, e in google_vertex_ai_endpoint.live : k => e.display_name } }
output "service_account" { value = google_service_account.live.email }
