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
