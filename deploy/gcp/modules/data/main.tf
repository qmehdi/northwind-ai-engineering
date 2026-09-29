# Data and governance: three buckets (data, model artifacts, pipeline root), the tickets as a
# BigQuery external table, and an optional Dataplex Universal Catalog entry. CMEK is a key
# name passed in; the course does not create a key ring.
variable "project" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "cmek_key" { type = string }
variable "dataplex_catalog" { type = bool }
variable "tickets_path" { type = string }
variable "golden_dir" { type = string }

locals {
  buckets = {
    data      = { versioning = false, description = "Datasets: tickets, golden sets, policy corpus" }
    artifacts = { versioning = true, description = "Model artifacts, prompt versions, the agent registry document, delivery artifacts" }
    pipelines = { versioning = false, description = "Pipeline root for Vertex AI Pipelines runs" }
  }
  dataset_id = "${replace(var.environment, "-", "_")}_platform"
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

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
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
output "tickets_table" { value = "${var.project}.${google_bigquery_dataset.platform.dataset_id}.${google_bigquery_table.tickets.table_id}" }
