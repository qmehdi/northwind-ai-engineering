# The one environment of the Google Cloud track (ADR 0008, 0009). Every name starts with
# `<environment>-<tenant>-`; `mode = "solo"` collapses the tenant list to the single tenant
# `solo`. Service account ids are capped at 30 characters by IAM, so they use the short form
# `nw-<tenant>-<kind>` while everything else carries the full prefix.

variable "project" {
  type        = string
  description = "Project id that holds the whole platform"
}

variable "project_number" {
  type        = string
  description = "Project number (gcloud projects describe <id> --format='value(projectNumber)'); names the Google service agents and the embedding publisher model without a data source, so `terraform plan` needs no credentials"
  validation {
    condition     = can(regex("^[0-9]{6,20}$", var.project_number))
    error_message = "project_number is the numeric project number."
  }
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "environment" {
  type        = string
  default     = "northwind"
  description = "The environment name that starts every resource name; a second deploy with another value is a second environment in the same project"
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,11}$", var.environment))
    error_message = "environment is 3 to 12 lowercase letters or digits, starting with a letter, so <environment>-<tenant>-<kind> fits every Google name limit."
  }
}

variable "mode" {
  type        = string
  default     = "cohort"
  description = "cohort: one tenant per learner from `tenants`; solo: the single tenant `solo`"
  validation {
    condition     = contains(["cohort", "solo"], var.mode)
    error_message = "mode is cohort or solo."
  }
}

variable "tenants" {
  type        = list(string)
  default     = []
  description = "Learner handles in cohort mode (2 to 16 lowercase letters or digits, starting with a letter; `live` is reserved for the promoted target)"
  validation {
    condition     = alltrue([for t in var.tenants : can(regex("^[a-z][a-z0-9]{1,15}$", t)) && t != "live"])
    error_message = "each tenant is 2 to 16 lowercase letters or digits, starts with a letter, and is not `live`."
  }
  validation {
    condition     = length(distinct(var.tenants)) == length(var.tenants)
    error_message = "tenants must be unique."
  }
}

variable "tenant_members" {
  type        = map(string)
  default     = {}
  description = "Optional tenant to Google account map (alice = \"alice@example.com\"): the account may impersonate its tenant identity service account"
}

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Tag of the course images in Artifact Registry; scripts/images_gcp.sh pushes the git SHA and latest"
}

variable "billing_account" {
  type        = string
  description = "Billing account id for the budget, like 012345-6789AB-CDEF01"
}

variable "budget_usd" {
  type    = number
  default = 300
}

variable "tenant_budget_usd" {
  type        = number
  default     = 25
  description = "Model spend cap per tenant on the gateway, reset every 30 days"
}

variable "alert_email" {
  type    = string
  default = ""
}

variable "cmek_key" {
  type        = string
  default     = ""
  description = "Optional Cloud KMS key (projects/../cryptoKeys/..) for the buckets and the BigQuery dataset; empty keeps Google-managed encryption"
}

variable "dataplex_catalog" {
  type        = bool
  default     = false
  description = "Register the tickets table in a Dataplex Universal Catalog entry group (off: the course does not need it, on: what a data governance team expects)"
}

variable "embedding_model" {
  type        = string
  default     = "text-embedding-005"
  description = "Publisher embedding model RAG Engine embeds with; its dimension (768) sizes the optional Vector Search index"
}

variable "rag_backend" {
  type        = string
  default     = "managed"
  description = "managed: RagManagedDb, no fixed cost; vector_search: a Vertex AI Vector Search index and endpoint per tenant, billed per node hour (deploy/COSTS.md says why managed is the default)"
  validation {
    condition     = contains(["managed", "vector_search"], var.rag_backend)
    error_message = "rag_backend is managed or vector_search."
  }
}

variable "rag_managed_db_tier" {
  type        = string
  default     = ""
  description = "Project-wide RagManagedDb tier: basic, scaled or unprovisioned; empty leaves the project default untouched"
  validation {
    condition     = contains(["", "basic", "scaled", "unprovisioned"], var.rag_managed_db_tier)
    error_message = "rag_managed_db_tier is empty, basic, scaled or unprovisioned."
  }
}

variable "live_models" {
  type        = list(string)
  default     = ["triage", "semantic"]
  description = "Registered model names that get a live Vertex endpoint with a traffic split"
}

variable "endpoint_id_base" {
  type        = number
  default     = 100000
  description = "Vertex endpoint ids must be numeric; live endpoints take base, base+1, ... in live_models order. Change it for a second environment in the same project"
}

variable "model_monitoring" {
  type        = bool
  default     = true
  description = "Create a Model Monitoring job on every live endpoint (a script: the provider has no resource for Model Monitoring v2)"
}

variable "public_services" {
  type        = bool
  default     = true
  description = "Cloud Run services take unauthenticated calls guarded by x-api-key (the guide's curl); false keeps them private to the agent's service account"
}

variable "scheduler_enabled" {
  type        = bool
  default     = false
  description = "Run the weekly retraining pipeline; the job exists paused otherwise"
}

variable "scheduler_cron" {
  type    = string
  default = "0 6 * * 1"
}

variable "gateway_image" {
  type        = string
  default     = "berriai/litellm:main-stable"
  description = "LiteLLM image path under ghcr.io, pulled through the remote repository (Cloud Run cannot pull ghcr.io directly); pin a version tag before a cohort"
}

variable "gateway_database" {
  type        = bool
  default     = true
  description = "Cloud SQL Postgres (db-f1-micro) behind the gateway so per-tenant virtual keys and budgets work; false serves the master key only (solo mode can live with that)"
}

variable "gateway_models" {
  type = map(object({
    model    = string
    location = string
  }))
  default = {
    workhorse = { model = "vertex_ai/openai/gpt-oss-120b-maas", location = "us-central1" }
    # gpt-oss-20b-maas retires on Google on 2026-10-21 (nw/config.py), so Economy is the 120b
    # route with the Economy token caps until a cheaper open model is generally available.
    economy = { model = "vertex_ai/openai/gpt-oss-120b-maas", location = "us-central1" }
    judge   = { model = "vertex_ai/claude-opus-5", location = "us-east5" }
  }
  description = "Model role to LiteLLM model route (ADR 0010); the location is where the publisher model is served"
}

variable "github_owner" {
  type        = string
  default     = ""
  description = "GitHub owner of the course repository; empty skips Cloud Build triggers (the Cloud Deploy pipeline is created regardless)"
}

variable "github_repo" {
  type    = string
  default = "northwind-ai-engineering"
}

variable "github_branch" {
  type    = string
  default = "main"
}

variable "github_app_installation_id" {
  type        = number
  default     = 0
  description = "Installation id of the Cloud Build GitHub app on the owner (from the connection's action_uri on first apply)"
}

variable "github_oauth_token_secret_version" {
  type        = string
  default     = ""
  description = "projects/<p>/secrets/<s>/versions/<v> holding the GitHub OAuth token for the Cloud Build connection"
}

variable "canary_percent" {
  type        = number
  default     = 10
  description = "First canary phase of the Cloud Deploy pipeline; the remaining traffic moves after the manual approval"
  validation {
    condition     = var.canary_percent > 0 && var.canary_percent < 100
    error_message = "canary_percent is between 1 and 99."
  }
}
