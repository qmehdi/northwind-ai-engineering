# Data and governance: three buckets (data, model artifacts, pipeline root), the tickets as a
# BigQuery external table, the capture dataset the live endpoints log into, and an optional
# Dataplex Universal Catalog entry. CMEK is a key name passed in; the course does not create a
# key ring.
#
# Isolation (ADR 0009): every tenant owns the managed folder `<environment>-<tenant>/` in the
# artifacts and pipelines buckets and nothing else. `baselines/` (the production summaries the
# gates compare against) and `agents/` (the registry document) are managed folders the platform
# writes and tenants only read; `clouddeploy/` belongs to delivery. No tenant identity holds a
# bucket-wide object role.
#
# Retention (the retention table in docs/governance): operational data 90 days (captured
# traffic, trajectories, traces, feedback, monitoring output, pipeline roots, the endpoints'
# request logs in BigQuery), audit and approval records 400 days, noncurrent artifact versions
# 30 days. Objects are deleted, not only moved to a colder class. Paths inside a tenant folder:
# `<environment>-<tenant>/{capture,trajectories,traces,feedback}/` and
# `<environment>-<tenant>/{audit,approvals}/`; trajectories, feedback and approvals are where the
# ops store writes (the same rules hold for `live`).
variable "project" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "cmek_key" { type = string }
variable "dataplex_catalog" { type = bool }
variable "tickets_path" { type = string }
variable "golden_dir" { type = string }
variable "tenants" { type = list(string) }
variable "tenant_users" {
  type        = map(string)
  description = "Tenant to tenant identity email (modules/identity)"
}

locals {
  prefixes = [for t in concat(var.tenants, ["live"]) : "${var.environment}-${t}/"]
  # Rules per bucket: age in days, action, optional prefixes. SetStorageClass moves cold data to
  # Nearline; Delete enforces the retention table.
  rules = {
    data = [
      { age = 90, action = "SetStorageClass", prefixes = [], noncurrent = false },
    ]
    artifacts = [
      { age = 90, action = "Delete", prefixes = concat(["monitoring/"], flatten([for p in local.prefixes : ["${p}capture/", "${p}trajectories/", "${p}traces/", "${p}feedback/"]])), noncurrent = false },
      { age = 400, action = "Delete", prefixes = flatten([for p in local.prefixes : ["${p}audit/", "${p}approvals/"]]), noncurrent = false },
      { age = 30, action = "Delete", prefixes = [], noncurrent = true },
    ]
    pipelines = [
      { age = 90, action = "Delete", prefixes = [], noncurrent = false },
    ]
  }
  buckets = {
    data      = { versioning = false, description = "Datasets: tickets, golden sets, policy corpus" }
    artifacts = { versioning = true, description = "Model artifacts, prompt versions, the agent registry document, delivery artifacts" }
    pipelines = { versioning = false, description = "Pipeline root for Vertex AI Pipelines runs" }
  }
  dataset_id         = "${replace(var.environment, "-", "_")}_platform"
  capture_dataset_id = "${replace(var.environment, "-", "_")}_capture"
  day_ms             = 86400000
}

resource "google_storage_bucket" "buckets" {
  for_each                    = local.buckets
  project                     = var.project
  name                        = "${var.environment}-${var.project}-${each.key}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
  labels                      = merge(var.labels, { area = "data", bucket = each.key })

  versioning {
    enabled = each.value.versioning
  }

  dynamic "encryption" {
    for_each = var.cmek_key == "" ? [] : [var.cmek_key]
    content {
      default_kms_key_name = encryption.value
    }
  }

  dynamic "lifecycle_rule" {
    for_each = local.rules[each.key]
    content {
      condition {
        age                        = lifecycle_rule.value.noncurrent ? null : lifecycle_rule.value.age
        days_since_noncurrent_time = lifecycle_rule.value.noncurrent ? lifecycle_rule.value.age : null
        with_state                 = lifecycle_rule.value.noncurrent ? "ARCHIVED" : "ANY"
        matches_prefix             = lifecycle_rule.value.prefixes
      }
      action {
        type          = lifecycle_rule.value.action
        storage_class = lifecycle_rule.value.action == "SetStorageClass" ? "NEARLINE" : null
      }
    }
  }
}

# ----- managed folders: one per tenant, and the platform-owned ones -------------------------------

locals {
  folders = merge(
    { for t in var.tenants : "artifacts:${t}" => { bucket = "artifacts", name = "${var.environment}-${t}/" } },
    { for t in var.tenants : "pipelines:${t}" => { bucket = "pipelines", name = "${var.environment}-${t}/" } },
    {
      "artifacts:live"        = { bucket = "artifacts", name = "${var.environment}-live/" }
      "artifacts:baselines"   = { bucket = "artifacts", name = "baselines/" }
      "artifacts:agents"      = { bucket = "artifacts", name = "agents/" }
      "artifacts:clouddeploy" = { bucket = "artifacts", name = "clouddeploy/" }
      "artifacts:monitoring"  = { bucket = "artifacts", name = "monitoring/" }
    },
  )
}

resource "google_storage_managed_folder" "folders" {
  for_each      = local.folders
  bucket        = google_storage_bucket.buckets[each.value.bucket].name
  name          = each.value.name
  force_destroy = true
}

# The ops store (nw/agent/opstore.py, NW_OPS_STORE=gs://<artifacts bucket>) writes
# `<environment>-<owner>/{trajectories,feedback,approvals}/` inside the owner's folder. Each kind
# is a nested managed folder so a grant can stop at a kind: the runtimes (agent engine, policy
# service, live services) write trajectories and feedback, and only the owner's approver writes
# approvals (claim markers, approval records, the escalation queue; ADR 0005). Grants on a
# parent folder reach its nested folders, so a runtime holds no write role on the owner folder.
locals {
  ops_kinds = ["trajectories", "feedback", "approvals"]
  ops_folders = {
    for pair in setproduct(concat(var.tenants, ["live"]), local.ops_kinds) :
    "ops:${pair[0]}:${pair[1]}" => "${var.environment}-${pair[0]}/${pair[1]}/"
  }
}

# The live endpoints' canary record `<environment>-live/endpoints/<name>.json` (nw/platform/gcp.py):
# which deployed model is stable and which is the canary, written by whoever moves the traffic
# (the live identity, and the tenants in the promotion drill; modules/live grants it).
resource "google_storage_managed_folder" "live_endpoints" {
  bucket        = google_storage_bucket.buckets["artifacts"].name
  name          = "${var.environment}-live/endpoints/"
  force_destroy = true
  depends_on    = [google_storage_managed_folder.folders]
}

resource "google_storage_managed_folder" "ops" {
  for_each      = local.ops_folders
  bucket        = google_storage_bucket.buckets["artifacts"].name
  name          = each.value
  force_destroy = true
  depends_on    = [google_storage_managed_folder.folders]
}

# The learner's own folders, read and write; the shared read-only ones; the tickets.
resource "google_storage_managed_folder_iam_member" "tenant_own" {
  for_each       = { for k, f in local.folders : k => f if contains(var.tenants, split(":", k)[1]) }
  bucket         = google_storage_managed_folder.folders[each.key].bucket
  managed_folder = google_storage_managed_folder.folders[each.key].name
  role           = "roles/storage.objectUser"
  member         = "serviceAccount:${var.tenant_users[split(":", each.key)[1]]}"
}

resource "google_storage_managed_folder_iam_member" "tenant_shared" {
  for_each       = { for pair in setproduct(var.tenants, ["baselines", "agents"]) : "${pair[0]}:${pair[1]}" => { tenant = pair[0], folder = "artifacts:${pair[1]}" } }
  bucket         = google_storage_managed_folder.folders[each.value.folder].bucket
  managed_folder = google_storage_managed_folder.folders[each.value.folder].name
  role           = "roles/storage.objectViewer"
  member         = "serviceAccount:${var.tenant_users[each.value.tenant]}"
}

resource "google_storage_bucket_iam_member" "tenant_data" {
  for_each = toset(var.tenants)
  bucket   = google_storage_bucket.buckets["data"].name
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${var.tenant_users[each.key]}"
}

# The synthetic tickets, the one dataset every project reads. Pipelines read the copy in the
# bucket; BigQuery reads it in place through the external table below.
resource "google_storage_bucket_object" "tickets" {
  bucket       = google_storage_bucket.buckets["data"].name
  name         = "tickets/tickets.jsonl"
  source       = var.tickets_path
  content_type = "application/x-ndjson"
}

# The committed production summaries the pipelines' gates compare against
# (`data/golden/*_production.json`), where the scheduler and `nw.platform.gcp` point
# `production_summary`: gs://<artifacts>/baselines/<pipeline>_production.json.
resource "google_storage_bucket_object" "baselines" {
  for_each     = toset(["triage", "semantic"])
  depends_on   = [google_storage_managed_folder.folders]
  bucket       = google_storage_bucket.buckets["artifacts"].name
  name         = "baselines/${each.key}_production.json"
  source       = "${var.golden_dir}/${each.key}_production.json"
  content_type = "application/json"
}

resource "google_bigquery_dataset" "platform" {
  project                    = var.project
  dataset_id                 = local.dataset_id
  friendly_name              = "${var.environment} platform"
  description                = "Tickets and evaluation tables of the ${var.environment} environment"
  location                   = var.region
  delete_contents_on_destroy = true
  labels                     = merge(var.labels, { area = "data" })

  dynamic "default_encryption_configuration" {
    for_each = var.cmek_key == "" ? [] : [var.cmek_key]
    content {
      kms_key_name = default_encryption_configuration.value
    }
  }
}

resource "google_bigquery_dataset_iam_member" "tenant" {
  for_each   = toset(var.tenants)
  project    = var.project
  dataset_id = google_bigquery_dataset.platform.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${var.tenant_users[each.key]}"
}

# What the live endpoints log (request and response at a 10 percent sample): the capture the
# retention table keeps for 90 days. Model Monitoring reads it. Platform-owned; no tenant grant.
resource "google_bigquery_dataset" "capture" {
  project                         = var.project
  dataset_id                      = local.capture_dataset_id
  friendly_name                   = "${var.environment} capture"
  description                     = "Request and response logs of the ${var.environment} live endpoints; tables and partitions expire after 90 days"
  location                        = var.region
  delete_contents_on_destroy      = true
  default_table_expiration_ms     = 90 * local.day_ms
  default_partition_expiration_ms = 90 * local.day_ms
  labels                          = merge(var.labels, { area = "data", retention = "90d" })

  dynamic "default_encryption_configuration" {
    for_each = var.cmek_key == "" ? [] : [var.cmek_key]
    content {
      kms_key_name = default_encryption_configuration.value
    }
  }
}

resource "google_bigquery_table" "tickets" {
  project             = var.project
  dataset_id          = google_bigquery_dataset.platform.dataset_id
  table_id            = "tickets"
  description         = "External table over gs://${google_storage_bucket.buckets["data"].name}/tickets/*.jsonl"
  deletion_protection = false
  labels              = merge(var.labels, { area = "data" })

  external_data_configuration {
    autodetect            = true
    source_format         = "NEWLINE_DELIMITED_JSON"
    ignore_unknown_values = true
    source_uris           = ["gs://${google_storage_bucket.buckets["data"].name}/tickets/*.jsonl"]
  }
  depends_on = [google_storage_bucket_object.tickets]
}

# Dataplex Universal Catalog: an entry group for the environment and one entry for the
# tickets table, so a governance team finds the dataset with its owner and description.
# BigQuery tables are also discovered automatically; this entry carries the course's own
# aspect data (owner, sensitivity), which discovery cannot know.
resource "google_dataplex_entry_group" "catalog" {
  count          = var.dataplex_catalog ? 1 : 0
  project        = var.project
  location       = var.region
  entry_group_id = "${var.environment}-catalog"
  display_name   = "${var.environment} datasets"
  description    = "Datasets of the ${var.environment} support intelligence platform"
  labels         = merge(var.labels, { area = "data" })
}

resource "google_dataplex_entry_type" "dataset" {
  count         = var.dataplex_catalog ? 1 : 0
  project       = var.project
  location      = var.region
  entry_type_id = "${var.environment}-dataset"
  display_name  = "${var.environment} dataset"
  description   = "A dataset the course trains or evaluates on"
  platform      = "BigQuery"
  system        = "BigQuery"
  type_aliases  = ["TABLE"]
  labels        = merge(var.labels, { area = "data" })
}

resource "google_dataplex_entry" "tickets" {
  count          = var.dataplex_catalog ? 1 : 0
  project        = var.project
  location       = var.region
  entry_group_id = google_dataplex_entry_group.catalog[0].entry_group_id
  entry_id       = "${var.environment}-tickets"
  entry_type     = google_dataplex_entry_type.dataset[0].name
  entry_source {
    display_name = "Northwind support tickets"
    description  = "Synthetic support tickets with type, queue, priority, tags and a reference answer (data/README.md)"
    platform     = "BigQuery"
    system       = "BigQuery"
    resource     = "//bigquery.googleapis.com/projects/${var.project}/datasets/${google_bigquery_dataset.platform.dataset_id}/tables/${google_bigquery_table.tickets.table_id}"
    labels       = { owner = "support-intelligence", sensitivity = "synthetic" }
  }
}

output "data_bucket" { value = google_storage_bucket.buckets["data"].name }
output "artifacts_bucket" { value = google_storage_bucket.buckets["artifacts"].name }
output "pipelines_bucket" { value = google_storage_bucket.buckets["pipelines"].name }
output "baselines_uri" { value = "gs://${google_storage_bucket.buckets["artifacts"].name}/baselines/" }
output "production_summaries" { value = { for k, o in google_storage_bucket_object.baselines : k => "gs://${o.bucket}/${o.name}" } }
output "tickets_uri" { value = "gs://${google_storage_bucket.buckets["data"].name}/${google_storage_bucket_object.tickets.name}" }
output "dataset_id" { value = google_bigquery_dataset.platform.dataset_id }
output "capture_dataset_id" { value = google_bigquery_dataset.capture.dataset_id }
output "folders" {
  description = "Managed folder names by key (artifacts:<tenant>, pipelines:<tenant>, artifacts:baselines, ..., ops:<owner>:<kind>)"
  value = merge(
    { for k, f in google_storage_managed_folder.folders : k => f.name },
    { for k, f in google_storage_managed_folder.ops : k => f.name },
    { "live:endpoints" = google_storage_managed_folder.live_endpoints.name },
  )
}
output "ops_store" {
  description = "NW_OPS_STORE for every runtime: keys land in <environment>-<owner>/<kind>/"
  value       = "gs://${google_storage_bucket.buckets["artifacts"].name}"
}
output "tickets_table" { value = "${var.project}.${google_bigquery_dataset.platform.dataset_id}.${google_bigquery_table.tickets.table_id}" }
