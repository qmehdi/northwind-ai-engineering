// The solo fixture: the tenant list is ignored and the one tenant is `solo`; LiteLLM as the
// gateway, so both gateway kinds compile.
using '../main.bicep'

param mode = 'solo'
param tenants = ['ignored']
param gatewayKind = 'litellm'
param endpointScope = 'a1b2c'
param apiKey = 'fixture-only-not-a-key'
param litellmMasterKey = 'fixture-only-not-a-key'
param postgresPassword = 'fixture-only-not-a-key'
param gatewayKeys = {
  solo: 'fixture-only-not-a-key'
  live: 'fixture-only-not-a-key'
}
