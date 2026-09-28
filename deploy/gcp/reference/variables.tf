variable "project" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "image_tag" {
  type    = string
  default = "latest"
}
variable "enable_pgvector" {
  type        = bool
  default     = false
  description = "Cloud SQL Postgres with pgvector as the managed retriever (adds a fixed monthly cost)"
}
variable "stage" {
  type        = string
  default     = ""
  description = "The stage the session tier was applied with; empty keeps the guide's names"
  validation {
    condition     = can(regex("^[a-z0-9]{0,7}$", var.stage))
    error_message = "stage is lowercase letters and digits, at most 7 characters."
  }
}
