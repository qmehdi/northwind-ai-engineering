# Tracking and registry. Vertex AI Experiments needs no resource (a run names its experiment
# and the Agent Platform creates it) and the Model Registry is populated by the pipelines, so
# this module holds what has to exist before the first run: a least-privilege service account
# per tenant that pipelines run as, and a weekly retraining schedule per tenant, paused until
# scheduler_enabled is true.
variable "project" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "tenants" { type = list(string) }
variable "data_bucket" { type = string }
variable "artifacts_bucket" { type = string }
variable "pipelines_bucket" { type = string }
variable "production_summary" {
  type        = string
  description = "gs:// URI of the triage production summary the data module uploads to baselines/"
}
variable "scheduler_enabled" { type = bool }
variable "scheduler_cron" { type = string }

resource "google_service_account" "pipelines" {
  for_each     = toset(var.tenants)
  project      = var.project
  account_id   = "nw-${each.key}-pipelines"
  display_name = "${var.environment}-${each.key} pipelines"
}

# Pipeline steps train, register (aiplatform.user covers models.upload and pipeline jobs),
# read the tickets, write artifacts and the pipeline root, and log. No project-wide storage
# or editor role.
locals {
  project_roles = [
    "roles/aiplatform.user",
    "roles/bigquery.dataViewer",
    "roles/bigquery.jobUser",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/artifactregistry.reader",
  ]
  role_pairs = {
    for pair in setproduct(var.tenants, local.project_roles) : "${pair[0]}:${pair[1]}" => { tenant = pair[0], role = pair[1] }
  }
}

resource "google_project_iam_member" "pipelines" {
  for_each = local.role_pairs
  project  = var.project
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.pipelines[each.value.tenant].email}"
}

resource "google_storage_bucket_iam_member" "data" {
  for_each = toset(var.tenants)
  bucket   = var.data_bucket
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${google_service_account.pipelines[each.key].email}"
}

resource "google_storage_bucket_iam_member" "artifacts" {
  for_each = toset(var.tenants)
  bucket   = var.artifacts_bucket
  role     = "roles/storage.objectAdmin"
  member   = "serviceAccount:${google_service_account.pipelines[each.key].email}"
}

resource "google_storage_bucket_iam_member" "pipelines" {
  for_each = toset(var.tenants)
  bucket   = var.pipelines_bucket
  role     = "roles/storage.objectAdmin"
  member   = "serviceAccount:${google_service_account.pipelines[each.key].email}"
}

# The Agent Platform also has its own pipeline scheduler API (PipelineJob.create_schedule),
# which nw.platform.gcp can use from a notebook; the Cloud Scheduler job is the one that is
# infrastructure, reviewed and paused in Terraform. It posts the same body the SDK does to
# pipelineJobs.create, authenticated as the tenant's pipelines service account. The template
# is `retrain-triage.yaml`, which `make pipeline-compile` writes beside `triage.yaml` and
# `make pipeline-upload-gcp` (run by `make deploy-gcp`, and by every hand submission) copies to
# the tenant's artifacts prefix. The parameter names are the pipeline's (`nw/pipelines/params.py`);
# the values are the ones `nw.platform.gcp` defaults a hand submission to, and `trigger` tells the
# scheduled run apart on the registered version.
resource "google_cloud_scheduler_job" "retrain" {
  for_each         = toset(var.tenants)
  project          = var.project
  region           = var.region
  name             = "${var.environment}-${each.key}-retrain-triage"
  description      = "Weekly triage retraining candidate for ${var.environment}-${each.key}; the pipeline's gate decides, never this job"
  schedule         = var.scheduler_cron
  time_zone        = "Etc/UTC"
  paused           = !var.scheduler_enabled
  attempt_deadline = "180s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-aiplatform.googleapis.com/v1/projects/${var.project}/locations/${var.region}/pipelineJobs"
    headers     = { "Content-Type" = "application/json" }
    body = base64encode(jsonencode({
      displayName = "${var.environment}-${each.key}-retrain-triage"
      templateUri = "gs://${var.artifacts_bucket}/${var.environment}-${each.key}/pipelines/retrain-triage.yaml"
      runtimeConfig = {
        gcsOutputDirectory = "gs://${var.pipelines_bucket}/${var.environment}-${each.key}"
        parameterValues = {
          data_uri           = "gs://${var.data_bucket}/tickets/tickets.jsonl"
          output_root        = "gs://${var.artifacts_bucket}/${var.environment}-${each.key}/pipelines/runs"
          production_summary = var.production_summary
          tenant             = each.key
          environment        = var.environment
          trigger            = "schedule"
        }
      }
      serviceAccount = google_service_account.pipelines[each.key].email
      labels         = merge(var.labels, { tenant = each.key, trigger = "scheduler" })
    }))
    oauth_token {
      service_account_email = google_service_account.pipelines[each.key].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
}

output "service_accounts" { value = { for k, sa in google_service_account.pipelines : k => sa.email } }
output "scheduler_jobs" { value = { for k, j in google_cloud_scheduler_job.retrain : k => j.name } }
