# Tracking and registry. Vertex AI Experiments needs no resource (a run names its experiment
# and the Agent Platform creates it) and the Model Registry is populated by the pipelines, so
# this module holds what has to exist before the first run: a least-privilege service account
# per tenant that pipelines run as, and a weekly retraining schedule per tenant, paused until
# scheduler_enabled is true.
#
# The pipelines identity holds the custom role `<environment>_pipelines` (modules/identity): no
# direct model predict and no reasoning engine rights. It writes only its tenant's managed
# folders, reads the tickets and the platform's baselines, and may act as itself, which both the
# scheduler's pipelineJobs.create and the pipeline's own custom jobs need. The tenant identity
# may act as it, so a learner can submit a run.
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
variable "roles" { type = map(string) }
variable "tenant_users" { type = map(string) }
variable "folders" { type = map(string) }
variable "dataset_id" { type = string }
variable "scheduler_cron" { type = string }

resource "google_service_account" "pipelines" {
  for_each     = toset(var.tenants)
  project      = var.project
  account_id   = "nw-${each.key}-pipelines"
  display_name = "${var.environment}-${each.key} pipelines"
}

# Pipeline steps train, register (the custom role covers models.upload, custom jobs and pipeline
# jobs), read the tickets, write artifacts and the pipeline root, and log. No project-wide
# storage, BigQuery data or editor role.
locals {
  # Keys are static names: the custom role's id is only known after apply.
  project_roles = {
    pipelines = var.roles["pipelines"]
    jobuser   = "roles/bigquery.jobUser"
    logs      = "roles/logging.logWriter"
    metrics   = "roles/monitoring.metricWriter"
    images    = "roles/artifactregistry.reader"
  }
  role_pairs = {
    for pair in setproduct(var.tenants, keys(local.project_roles)) : "${pair[0]}:${pair[1]}" => { tenant = pair[0], role = local.project_roles[pair[1]] }
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

resource "google_bigquery_dataset_iam_member" "tickets" {
  for_each   = toset(var.tenants)
  project    = var.project
  dataset_id = var.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.pipelines[each.key].email}"
}

# Own folders read and write, the baselines read only. Nothing bucket wide. The artifacts folder
# covers what the steps read and write there: `source/` (the bundle the run executes), `rag/`,
# `registry/` and `agents/` (written with generation preconditions, deleted when superseded).
# The champion lookup reads the registry through models.get and models.list (the custom role).
locals {
  folder_grants = merge(
    { for t in var.tenants : "${t}:artifacts" => { tenant = t, bucket = var.artifacts_bucket, folder = var.folders["artifacts:${t}"], role = "roles/storage.objectUser" } },
    { for t in var.tenants : "${t}:pipelines" => { tenant = t, bucket = var.pipelines_bucket, folder = var.folders["pipelines:${t}"], role = "roles/storage.objectUser" } },
    { for t in var.tenants : "${t}:baselines" => { tenant = t, bucket = var.artifacts_bucket, folder = var.folders["artifacts:baselines"], role = "roles/storage.objectViewer" } },
  )
}

resource "google_storage_managed_folder_iam_member" "pipelines" {
  for_each       = local.folder_grants
  bucket         = each.value.bucket
  managed_folder = each.value.folder
  role           = each.value.role
  member         = "serviceAccount:${google_service_account.pipelines[each.value.tenant].email}"
}

# pipelineJobs.create with serviceAccount set to this account needs actAs on it: the scheduler
# job authenticates as the account itself, and the learner submits as the tenant identity.
resource "google_service_account_iam_member" "acts_as_self" {
  for_each           = toset(var.tenants)
  service_account_id = google_service_account.pipelines[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.pipelines[each.key].email}"
}

resource "google_service_account_iam_member" "tenant_acts_as" {
  for_each           = toset(var.tenants)
  service_account_id = google_service_account.pipelines[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.tenant_users[each.key]}"
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
# What the gate's champion lookup and the register step need to build this platform: the JSON
# nw.platform.gcp passes on a hand submission (`platform_env`, keys sorted as it sorts them).
locals {
  platform_env = {
    for t in var.tenants : t => jsonencode({
      NW_TRACK                = "gcp"
      NW_GCP_PROJECT          = var.project
      NW_GCP_RUN_REGION       = var.region
      NW_GCP_ARTIFACTS_BUCKET = var.artifacts_bucket
      NW_GCP_PIPELINES_BUCKET = var.pipelines_bucket
      NW_GCP_DATA_BUCKET      = var.data_bucket
      NW_ENVIRONMENT          = var.environment
      NW_TENANT               = t
    })
  }
}

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
          # The tenant's code, not the image's: every submission (and `make pipeline-upload-gcp`)
          # uploads the checkout's bundle as source/latest.tar.gz (nw/pipelines/source.py).
          source_uri   = "gs://${var.artifacts_bucket}/${var.environment}-${each.key}/source/latest.tar.gz"
          platform_env = local.platform_env[each.key]
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

  depends_on = [google_service_account_iam_member.acts_as_self, google_project_iam_member.pipelines]
}

output "service_accounts" { value = { for k, sa in google_service_account.pipelines : k => sa.email } }
output "scheduler_jobs" { value = { for k, j in google_cloud_scheduler_job.retrain : k => j.name } }
