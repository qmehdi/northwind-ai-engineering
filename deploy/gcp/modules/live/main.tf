# The live target of the platform: one Vertex endpoint per registered model name with a
# traffic split (the promotion drill deploys the approved version at canary_percent and moves
# the rest after the check), Model Monitoring on each, and the identity the live Cloud Run
# services run as (Cloud Deploy creates those services from deploy/gcp/platform/delivery).
#
# The live services call one another (the agent calls triage, semantic and policy), so the live
# identity is an invoker of every `<environment>-live-*` service through a conditional grant
# (the services do not exist until the first rollout). Tenants may deploy, undeploy and move
# traffic on the live endpoints for the promotion drill (a custom role granted on each endpoint)
# and nothing else live. Destroy undeploys every model first: an endpoint with deployed models
# cannot be deleted.
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
variable "gateway_key_secret" {
  type        = string
  description = "Secret id of the live services' gateway key (modules/gateway)"
}
variable "capture_dataset_id" {
  type        = string
  description = "BigQuery dataset the endpoints log requests into; created by modules/data first"
}
variable "roles" { type = map(string) }
variable "tenant_users" { type = map(string) }
variable "folders" {
  type        = map(string)
  description = "Managed folder names by key (modules/data), for the live owner's ops folders"
}
variable "approvers" {
  type        = list(string)
  default     = []
  description = "Members (user:, group:) who may impersonate the approvers identity and approve the live runtime's proposals"
}
variable "tenants_for_drill" {
  type        = list(string)
  description = "Tenants that run the promotion drill on the live endpoints"
}

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
      # The dataset id comes from modules/data's resource, so the endpoint waits for it.
      output_uri = "bq://${var.project}.${var.capture_dataset_id}.live_${each.key}_requests"
    }
  }
}

# An endpoint with deployed models cannot be deleted: undeploy every model first. This resource
# depends on the endpoint, so Terraform destroys it (and runs the command) before the endpoint.
resource "null_resource" "undeploy_on_destroy" {
  for_each = google_vertex_ai_endpoint.live
  triggers = {
    endpoint = each.value.name
    project  = var.project
    region   = var.region
  }
  provisioner "local-exec" {
    when    = destroy
    command = "for m in $(gcloud ai endpoints describe ${self.triggers.endpoint} --project ${self.triggers.project} --region ${self.triggers.region} --format='value(deployedModels[].id)' 2>/dev/null | tr ';' ' '); do gcloud ai endpoints undeploy-model ${self.triggers.endpoint} --project ${self.triggers.project} --region ${self.triggers.region} --deployed-model-id=$m --quiet; done"
  }
}

resource "google_vertex_ai_endpoint_iam_member" "tenant_drill" {
  provider = google-beta
  for_each = { for pair in setproduct(var.tenants_for_drill, keys(google_vertex_ai_endpoint.live)) : "${pair[0]}:${pair[1]}" => { tenant = pair[0], model = pair[1] } }
  project  = var.project
  location = var.region
  endpoint = google_vertex_ai_endpoint.live[each.value.model].name
  role     = var.roles["live_deployer"]
  member   = "serviceAccount:${var.tenant_users[each.value.tenant]}"
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

# No roles/aiplatform.user: the live services reach models through the gateway with the live
# key; the policy service retrieves through the RAG reader role.
resource "google_project_iam_member" "live" {
  for_each = { trace = "roles/cloudtrace.agent", metrics = "roles/monitoring.metricWriter", logs = "roles/logging.logWriter", rag = var.roles["rag_reader"] }
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.live.email}"
}

resource "google_project_iam_member" "live_invoker" {
  project = var.project
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.live.email}"
  condition {
    title       = "live services only"
    description = "The live services call one another; nothing else"
    # extract() sidesteps whether the name carries the project id or number.
    expression = "resource.name.extract(\"/services/{service}\").startsWith(\"${var.environment}-live-\")"
  }
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

resource "google_secret_manager_secret_iam_member" "live_gateway_key" {
  project   = var.project
  secret_id = var.gateway_key_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.live.email}"
}

# The live identity moves traffic and undeploys superseded models on the live endpoints
# (nw.platform.gcp: promote, rollback, undeploy of old replicas) with the same endpoint-scoped
# role the drill tenants hold, and writes the canary record beside them.
resource "google_vertex_ai_endpoint_iam_member" "live_deployer" {
  provider = google-beta
  for_each = google_vertex_ai_endpoint.live
  project  = var.project
  location = var.region
  endpoint = each.value.name
  role     = var.roles["live_deployer"]
  member   = "serviceAccount:${google_service_account.live.email}"
}

resource "google_storage_managed_folder_iam_member" "canary_record" {
  for_each       = merge({ live = "serviceAccount:${google_service_account.live.email}" }, { for t in var.tenants_for_drill : t => "serviceAccount:${var.tenant_users[t]}" })
  bucket         = var.artifacts_bucket
  managed_folder = var.folders["live:endpoints"]
  role           = "roles/storage.objectUser"
  member         = each.value
}

# ----- ops state of the live services and the approval gate's platform half (ADR 0005) -------
# The live services keep trajectories and feedback in `<environment>-live/` (NW_OPS_STORE). They
# may write those two folders and nothing else of it; `approvals/` (claim markers, approval
# records, the escalation queue) belongs to the approvers identity, which a person impersonates
# to run `make approve` with NW_TENANT=live. The approval executes the recorded escalation as
# that identity, so the platform, not only the loop, decides who can queue an escalation.
resource "google_storage_managed_folder_iam_member" "live_ops" {
  for_each       = toset(["trajectories", "feedback"])
  bucket         = var.artifacts_bucket
  managed_folder = var.folders["ops:live:${each.key}"]
  role           = "roles/storage.objectUser"
  member         = "serviceAccount:${google_service_account.live.email}"
}

resource "google_service_account" "approvers" {
  project      = var.project
  account_id   = "${var.environment}-approvers"
  display_name = "${var.environment} approvers of the live agent's proposals"
}

resource "google_storage_managed_folder_iam_member" "approvers" {
  for_each       = toset(["approvals", "trajectories"])
  bucket         = var.artifacts_bucket
  managed_folder = var.folders["ops:live:${each.key}"]
  role           = "roles/storage.objectUser"
  member         = "serviceAccount:${google_service_account.approvers.email}"
}

resource "google_service_account_iam_member" "approvers" {
  for_each           = toset(var.approvers)
  service_account_id = google_service_account.approvers.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = each.value
}

output "approvers_service_account" { value = google_service_account.approvers.email }
output "endpoint_ids" { value = { for k, e in google_vertex_ai_endpoint.live : k => e.name } }
output "endpoint_names" { value = { for k, e in google_vertex_ai_endpoint.live : k => e.display_name } }
output "service_account" { value = google_service_account.live.email }
