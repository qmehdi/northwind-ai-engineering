# Terraform 1.11 or later: the secrets are write-only (`secret_data_wo`, `password_wo`) from
# ephemeral random values (Terraform 1.10), so no key or password is ever in the state or the
# plan file.
#
# State lives in a Cloud Storage bucket with object versioning (`scripts/deploy_gcp.sh
# state-bucket` creates it, CMEK optional through NW_TF_STATE_KMS_KEY) and is configured at init:
#   terraform init -backend-config=bucket=<project>-<environment>-tfstate -backend-config=prefix=platform
# Solo mode may keep local state: NW_TF_STATE=local makes scripts/deploy_gcp.sh write
# backend_override.tf with a local backend, which Terraform merges over this block.
terraform {
  required_version = ">= 1.11"
  backend "gcs" {}
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 8.4, < 9.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 8.4, < 9.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.7"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.2"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

provider "google-beta" {
  project = var.project
  region  = var.region
}
