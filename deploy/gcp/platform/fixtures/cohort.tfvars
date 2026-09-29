# Two tenants, used by tests/platform/test_gcp_terraform.py; never applied.
project                           = "northwind-fixture"
project_number                    = "123456789012"
region                            = "us-central1"
mode                              = "cohort"
tenants                           = ["alice", "bob"]
billing_account                   = "012345-6789AB-CDEF01"
alert_email                       = "ops@example.com"
image_tag                         = "abc1234"
github_owner                      = "example"
github_app_installation_id        = 123
github_oauth_token_secret_version = "projects/northwind-fixture/secrets/github-token/versions/1"
