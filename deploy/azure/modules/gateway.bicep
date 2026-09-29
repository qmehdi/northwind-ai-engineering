// The model gateway: the one door every model call goes through (ADR 0008). Two kinds, one
// parameter:
//
//   apim     API Management's AI gateway in front of Foundry (the default). Two APIs publish
//            the Foundry shapes the course client speaks: `<gateway>/openai/v1` (gpt-oss-120b,
//            mistral-small-2503) and `<gateway>/anthropic` (Claude). A subscription per tenant is the
//            tenant's key (header `api-key`); llm-token-limit caps tokens per minute per
//            subscription, llm-emit-token-metric sends tokens per tenant to Application
//            Insights, a backend pool load balances across Foundry deployments (a second
//            Foundry resource joins with `secondaryFoundryEndpoint`) with a circuit breaker,
//            and APIM calls Foundry with its own managed identity. Semantic caching is off: it
//            needs an Azure Managed Redis cache and an embeddings deployment, and the course's
//            prompts rarely repeat.
//   litellm  LiteLLM on Container Apps with PostgreSQL for virtual keys, the same proxy as the
//            AWS and Google Cloud tracks, for an organisation that does not want APIM.
//
// Either way every owner's key lands in Key Vault as `<environment>-<owner>-gateway-key`.
metadata owner = 'northwind'

param environment string
param location string
param tags object
param suffix string
param kind string
param owners array
param apimSku string
param publisherEmail string
param tenantTokensPerMinute int
param foundryAccountName string
param foundryEndpoint string
param secondaryFoundryEndpoint string
param keyVaultName string
param appInsightsId string
@secure()
param appInsightsConnectionString string
param containerAppsEnvironmentId string
param models array
@secure()
param litellmMasterKey string
@secure()
param postgresPassword string
@secure()
param gatewayKeys object
param enableTelemetry bool

var apim = kind == 'apim'
var apimName = '${environment}-apim-${suffix}'
var cognitiveServicesUser = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var v2 = endsWith(apimSku, 'V2')

resource vault 'Microsoft.KeyVault/vaults@2026-02-01' existing = {
  name: keyVaultName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2026-07-01' existing = {
  name: foundryAccountName
}

// ----- API Management -------------------------------------------------------------------------

module apimService 'br/public:avm/res/api-management/service:0.14.4' = if (apim) {
  name: '${environment}-apim'
  params: {
    // Global host name: <name>.azure-api.net, 1 to 50 characters.
    name: apimName
    location: location
    tags: tags
    sku: apimSku
    skuCapacity: 1
    publisherEmail: publisherEmail
    publisherName: '${environment} platform'
    managedIdentities: {
      systemAssigned: true
    }
    enableDeveloperPortal: false
    enableTelemetry: enableTelemetry
  }
}

resource service 'Microsoft.ApiManagement/service@2024-05-01' existing = if (apim) {
  name: apimName
  dependsOn: [apimService]
}

// APIM reaches Foundry as itself: Cognitive Services User on the Foundry resource.
resource apimCallsFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (apim) {
  name: guid(foundry.id, apimName, cognitiveServicesUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUser)
    principalId: apim ? apimService!.outputs.systemAssignedMIPrincipalId! : ''
    principalType: 'ServicePrincipal'
  }
}

resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = if (apim) {
  parent: service
  name: 'appinsights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: appInsightsId
    credentials: {
      connectionString: appInsightsConnectionString
    }
  }
}

// `metrics: true` is what lets llm-emit-token-metric reach Application Insights.
resource diagnostics 'Microsoft.ApiManagement/service/diagnostics@2024-05-01' = if (apim) {
  parent: service
  name: 'applicationinsights'
  properties: {
    loggerId: logger.id
    alwaysLog: 'allErrors'
    metrics: true
    verbosity: 'information'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
  }
}

// One backend per Foundry resource and API shape. The circuit breaker trips on 429 and 5xx and
// honours Retry-After, so the pool sends traffic to the next member meanwhile.
var foundryRoots = empty(secondaryFoundryEndpoint)
  ? [foundryEndpoint]
  : [foundryEndpoint, secondaryFoundryEndpoint]
var shapes = [
  { api: 'openai', path: 'openai', resource: 'https://cognitiveservices.azure.com' }
  { api: 'anthropic', path: 'anthropic', resource: 'https://ai.azure.com' }
]
var backendMembers = flatten(map(shapes, s => map(range(0, length(foundryRoots)), i => {
  name: '${s.api}-${i}'
  api: s.api
  url: '${foundryRoots[i]}/${s.path}'
  priority: i + 1
})))

resource backends 'Microsoft.ApiManagement/service/backends@2024-05-01' = [
  for b in backendMembers: if (apim) {
    parent: service
    name: 'foundry-${b.name}'
    properties: {
      protocol: 'http'
      url: b.url
      description: 'Foundry ${b.api} shape, member ${b.priority}'
      circuitBreaker: {
        rules: [
          {
            name: 'throttled-or-failing'
            failureCondition: {
              count: 3
              interval: 'PT1M'
              statusCodeRanges: [
                { min: 429, max: 429 }
                { min: 500, max: 599 }
              ]
            }
            tripDuration: 'PT1M'
            acceptRetryAfter: true
          }
        ]
      }
    }
  }
]

resource pools 'Microsoft.ApiManagement/service/backends@2024-05-01' = [
  for s in shapes: if (apim) {
    parent: service
    name: 'foundry-${s.api}-pool'
    properties: {
      type: 'Pool'
      description: 'Load balancing across Foundry deployments for the ${s.api} shape (priority order)'
      pool: {
        services: [
          for m in filter(backendMembers, b => b.api == s.api): {
            id: '/backends/foundry-${m.name}'
            priority: m.priority
            weight: 1
          }
        ]
      }
    }
    dependsOn: [backends]
  }
]

resource apis 'Microsoft.ApiManagement/service/apis@2024-05-01' = [
  for s in shapes: if (apim) {
    parent: service
    name: 'foundry-${s.api}'
    properties: {
      displayName: 'Foundry ${s.api}'
      path: s.path
      protocols: ['https']
      serviceUrl: '${foundryEndpoint}/${s.path}'
      subscriptionRequired: true
      subscriptionKeyParameterNames: {
        header: 'api-key'
        query: 'subscription-key'
      }
      apiType: 'http'
    }
  }
]

// Pass-through operations: the Foundry path after /openai or /anthropic is forwarded as is.
var operations = [
  { name: 'post', method: 'POST' }
  { name: 'get', method: 'GET' }
]
var apiOperations = flatten(map(range(0, length(shapes)), i => map(operations, o => {
  api: i
  name: o.name
  method: o.method
})))

resource apiOperationsResources 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [
  for op in apiOperations: if (apim) {
    parent: apis[op.api]
    name: op.name
    properties: {
      displayName: '${op.method} any path'
      method: op.method
      urlTemplate: '/*'
    }
  }
]

// The AI gateway policy: the token limit is per subscription (the counter is shared by both
// APIs because the key is the same), the token metric carries the tenant, the request goes to
// the pool with APIM's identity and is retried once on the next member on 429.
var tokenPolicies = '''
    <llm-token-limit counter-key="@(context.Subscription.Id)" tokens-per-minute="__TPM__" estimate-prompt-tokens="false" remaining-tokens-variable-name="remainingTokens" />
    <llm-emit-token-metric namespace="__NAMESPACE__">
      <dimension name="Subscription ID" />
      <dimension name="API ID" />
      <dimension name="tenant" value="@(context.Subscription?.Name ?? &quot;none&quot;)" />
    </llm-emit-token-metric>'''
var rateLimitPolicy = '''
    <rate-limit-by-key calls="60" renewal-period="60" counter-key="@(context.Subscription.Id)" />'''
var policyTemplate = '''<policies>
  <inbound>
    <base />
    <set-backend-service backend-id="__POOL__" />
    <authentication-managed-identity resource="__RESOURCE__" />
    <set-header name="api-key" exists-action="delete" />
    <set-header name="x-api-key" exists-action="delete" />__LIMITS__
  </inbound>
  <backend>
    <retry condition="@(context.Response.StatusCode == 429)" count="1" interval="1" first-fast-retry="true">
      <forward-request buffer-request-body="true" />
    </retry>
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>'''

resource apiPolicies 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = [
  for (s, i) in shapes: if (apim) {
    parent: apis[i]
    name: 'policy'
    properties: {
      format: 'rawxml'
      // The LLM policies cover the Anthropic Messages shape on the v2 tiers only; a classic
      // tier gets a request rate limit on that API instead.
      value: replace(
        replace(
          replace(
            replace(
              replace(
                policyTemplate,
                '__LIMITS__',
                s.api == 'openai' || v2 ? tokenPolicies : rateLimitPolicy
              ),
              '__TPM__',
              string(tenantTokensPerMinute)
            ),
            '__NAMESPACE__',
            environment
          ),
          '__POOL__',
          'foundry-${s.api}-pool'
        ),
        '__RESOURCE__',
        s.resource
      )
    }
    dependsOn: [pools, diagnostics]
  }
]

resource product 'Microsoft.ApiManagement/service/products@2024-05-01' = if (apim) {
  parent: service
  name: '${environment}-tenants'
  properties: {
    displayName: '${environment} tenants'
    description: 'Every course owner (tenants and live) subscribes here; the key is the tenant key'
    subscriptionRequired: true
    approvalRequired: false
    state: 'published'
  }
}

resource productApis 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = [
  for (s, i) in shapes: if (apim) {
    parent: product
    name: apis[i].name
  }
]

resource subscriptions 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = [
  for o in owners: if (apim) {
    parent: service
    name: '${environment}-${o}'
    properties: {
      displayName: '${environment}-${o}'
      scope: product.id
      state: 'active'
      allowTracing: false
    }
  }
]

// The subscription key is the owner's gateway key, kept in Key Vault and injected by reference.
resource apimKeys 'Microsoft.KeyVault/vaults/secrets@2026-02-01' = [
  for (o, i) in owners: if (apim) {
    parent: vault
    name: '${environment}-${o}-gateway-key'
    properties: {
      value: subscriptions[i].listSecrets().primaryKey
      contentType: 'API Management subscription key for ${environment}-${o}'
    }
  }
]

// ----- LiteLLM on Container Apps ----------------------------------------------------------------

var litellmModels = [
  for m in models: {
    model_name: m.name
    litellm_params: m.format == 'Anthropic'
      ? {
          model: 'azure_ai/${m.name}'
          api_base: '${foundryEndpoint}/anthropic'
        }
      : {
          model: 'azure/${m.name}'
          api_base: '${foundryEndpoint}/openai/v1'
          api_version: 'preview'
        }
    model_info: {
      role: m.role
      environment: environment
    }
  }
]
var litellmConfig = {
  model_list: litellmModels
  litellm_settings: {
    drop_params: true
    request_timeout: 120
    num_retries: 2
    turn_off_message_logging: true
    // Entra ID with the gateway's managed identity (AZURE_CLIENT_ID), no Foundry key.
    enable_azure_ad_token_refresh: true
  }
  general_settings: {
    master_key: 'os.environ/LITELLM_MASTER_KEY'
    database_url: 'os.environ/DATABASE_URL'
    store_model_in_db: false
  }
}

module gatewayIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = if (!apim) {
  name: '${environment}-gateway-identity'
  params: {
    name: '${environment}-gateway-id'
    location: location
    tags: tags
    enableTelemetry: enableTelemetry
  }
}

resource litellmCallsFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!apim) {
  name: guid(foundry.id, '${environment}-gateway-id', cognitiveServicesUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUser)
    principalId: !apim ? gatewayIdentity!.outputs.principalId : ''
    principalType: 'ServicePrincipal'
  }
}

module database 'br/public:avm/res/db-for-postgre-sql/flexible-server:0.16.1' = if (!apim) {
  name: '${environment}-gateway-db'
  params: {
    name: '${environment}-gateway-db-${suffix}'
    location: location
    tags: tags
    skuName: 'Standard_B1ms'
    tier: 'Burstable'
    availabilityZone: -1
    highAvailability: 'Disabled'
    storageSizeGB: 32
    version: '16'
    administratorLogin: 'litellm'
    administratorLoginPassword: postgresPassword
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
    publicNetworkAccess: 'Enabled'
    firewallRules: [
      {
        name: 'azure-services'
        startIpAddress: '0.0.0.0'
        endIpAddress: '0.0.0.0'
      }
    ]
    databases: [
      {
        name: 'litellm'
      }
    ]
    enableTelemetry: enableTelemetry
  }
}

module litellm 'br/public:avm/res/app/container-app:0.23.0' = if (!apim) {
  name: '${environment}-gateway-app'
  params: {
    name: '${environment}-gateway'
    location: location
    tags: tags
    environmentResourceId: containerAppsEnvironmentId
    managedIdentities: {
      userAssignedResourceIds: [gatewayIdentity!.outputs.resourceId]
    }
    ingressExternal: true
    ingressTargetPort: 4000
    ingressAllowInsecure: false
    scaleSettings: {
      minReplicas: 0
      maxReplicas: 3
    }
    secrets: [
      { name: 'master-key', value: litellmMasterKey }
      {
        name: 'database-url'
        value: 'postgresql://litellm:${postgresPassword}@${environment}-gateway-db-${suffix}.postgres.database.azure.com:5432/litellm?sslmode=require'
      }
      { name: 'config', value: string(litellmConfig) }
    ]
    volumes: [
      {
        name: 'config'
        storageType: 'Secret'
        secrets: [
          { secretRef: 'config', path: 'config.yaml' }
        ]
      }
    ]
    containers: [
      {
        name: 'litellm'
        image: 'ghcr.io/berriai/litellm:main-stable'
        args: ['--config', '/config/config.yaml', '--port', '4000']
        resources: {
          cpu: json('1.0')
          memory: '2Gi'
        }
        env: [
          { name: 'LITELLM_MASTER_KEY', secretRef: 'master-key' }
          { name: 'DATABASE_URL', secretRef: 'database-url' }
          { name: 'AZURE_CLIENT_ID', value: gatewayIdentity!.outputs.clientId }
          { name: 'STORE_MODEL_IN_DB', value: 'False' }
          { name: 'LITELLM_LOG', value: 'INFO' }
        ]
        volumeMounts: [
          { volumeName: 'config', mountPath: '/config' }
        ]
      }
    ]
    enableTelemetry: enableTelemetry
  }
  dependsOn: [database]
}

resource litellmMaster 'Microsoft.KeyVault/vaults/secrets@2026-02-01' = if (!apim) {
  parent: vault
  name: '${environment}-gateway-master-key'
  properties: {
    value: litellmMasterKey
    contentType: 'LiteLLM master key; scripts/deploy_azure.sh keys registers the owner keys with it'
  }
}

// LiteLLM virtual keys are chosen by the deploy script (kept in Key Vault across deploys) and
// registered with the proxy by `scripts/deploy_azure.sh keys`, with the owner's budget.
resource litellmKeys 'Microsoft.KeyVault/vaults/secrets@2026-02-01' = [
  for o in owners: if (!apim) {
    parent: vault
    name: '${environment}-${o}-gateway-key'
    properties: {
      value: gatewayKeys[?o] ?? ''
      contentType: 'LiteLLM virtual key for ${environment}-${o}'
    }
  }
]

// What the services call: the APIM gateway, or the LiteLLM app. The two go to different
// variables because they take different requests: NW_AZURE_APIM_GATEWAY_URL (Foundry's own
// shapes, the subscription key in `api-key`) and NW_GATEWAY_URL (LiteLLM, a bearer key).
output url string = apim ? 'https://${apimName}.azure-api.net' : 'https://${litellm!.outputs.fqdn}'
output apimGatewayUrl string = apim ? 'https://${apimName}.azure-api.net' : ''
output litellmUrl string = apim ? '' : 'https://${litellm!.outputs.fqdn}'
output apimName string = apim ? apimName : ''
output keySecretNames array = [for o in owners: '${environment}-${o}-gateway-key']
