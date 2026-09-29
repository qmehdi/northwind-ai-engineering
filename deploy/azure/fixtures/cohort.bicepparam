// The cohort fixture of tests/platform/test_azure_bicep.py: two tenants, APIM, the defaults.
using '../main.bicep'

param mode = 'cohort'
param tenants = ['alice', 'bob']
param alertEmail = 'instructor@example.com'
param endpointScope = 'a1b2c'
param apiKey = 'fixture-only-not-a-key'
