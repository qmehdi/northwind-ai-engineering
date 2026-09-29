terraform {
  required_version = ">= 1.9"
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
      version = ">= 3.6"
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
