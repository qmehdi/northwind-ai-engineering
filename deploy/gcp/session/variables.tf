variable "project" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "image_tag" {
  type    = string
  default = "latest"
}
variable "billing_account" {
  type        = string
  description = "Billing account id for the budget, like 012345-6789AB-CDEF01"
}
variable "budget_usd" {
  type    = number
  default = 100
}
variable "alert_email" {
  type    = string
  default = ""
}
variable "stage" {
  type        = string
  default     = ""
  description = "Put after `northwind` in every name so dev, staging and prod can share a project; empty keeps the guide's names"
  validation {
    condition     = can(regex("^[a-z0-9]{0,7}$", var.stage))
    error_message = "stage is lowercase letters and digits, at most 7 characters."
  }
}
variable "canary_percent" {
  type        = number
  default     = 0
  description = "Traffic share for the newest revision of every service; make deploy-gcp CANARY=10"
}
