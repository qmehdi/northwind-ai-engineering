# Solo mode: one tenant named solo, no gateway database, no triggers.
project          = "northwind-fixture"
project_number   = "123456789012"
mode             = "solo"
tenants          = ["ignored"]
billing_account  = "012345-6789AB-CDEF01"
image_tag        = "abc1234-dirty-1a2b3c4d"
gateway_database = false
