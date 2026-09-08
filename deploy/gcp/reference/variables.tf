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
