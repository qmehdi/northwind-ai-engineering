// The Azure platform of the course: one environment in one resource group (ADR 0008, 0013),
// every learner a tenant on it (cohort) or one tenant named `solo` (ADR 0009).
//
//   make build-azure                          # bicep build and lint, no Azure call
//   make deploy-azure NW_TENANTS=alice,bob    # cohort: a tenant per learner
//   make deploy-azure NW_MODE=solo            # one tenant named solo, in your own subscription
//
// Areas, one module each (deploy/azure/modules), in the order of the reference architecture:
//   network        the virtual network of the apps: egress limited to Azure, the private gateway database
//   policy         allowed sizes for Azure ML computes and online deployments (Azure Policy)
//   observability  Log Analytics, Application Insights, action group, budget, workbook, drift alert
//   data           the lake (Storage with hierarchical namespace), Purview optional and off
//   tracking       Azure ML workspace, Key Vault, ACR, training cluster, datastores, identities, roles
//   retrieval      Azure AI Search (indexes per tenant are created by the deploy script)
//   foundry        Foundry resource and project, three model deployments, Prompt Shields
//   gateway        API Management AI gateway (or LiteLLM on Container Apps), a key per owner
//   tenant         per owner: online endpoint, retraining schedule, least-privilege roles
//   agents         per owner: policy, agent and MCP apps on Container Apps (live: traffic split)
//   serving        the alerts the canary is judged by
//   delivery       the deployer identity with federated credentials, its custom role
//   defender       Defender for Containers (subscription scope, optional and off)
//
// Names: `<environment>-<owner>-<kind>` where hyphens are allowed; `nw<kind><suffix>` where they
// are not (storage, Key Vault, container registry); resources whose names are global or regional
// DNS labels carry `<suffix>`, six characters of uniqueString(resource group id, environment).
// `live` is the promoted target's owner and cannot be a tenant.
targetScope = 'resourceGroup'

@description('Prefix of every name; `northwind` in the course. A higher environment is a second deployment with another value.')
@minLength(2)
@maxLength(10)
param environment string = 'northwind'

@description('Region of every resource. eastus2 has every model the roles need (Claude, gpt-oss-120b, mistral-small-2503).')
param location string = resourceGroup().location

@allowed(['cohort', 'solo'])
param mode string = 'cohort'

@description('Cohort tenants: lowercase letters and digits, 2 to 12 characters each (the deploy script checks).')
param tenants array = []

@description('Optional: tenant to the Entra object id of the learner, who then gets the same roles as the tenant identity.')
param tenantUsers object = {}

param alertEmail string = ''
param budgetUsd int = 300
param budgetStartDate string = '${utcNow('yyyy-MM')}-01'

@description('Image tag in the platform registry; empty deploys a public placeholder image until `make images-azure` has run.')
param imageTag string = ''

@allowed(['apim', 'litellm'])
param gatewayKind string = 'apim'

@description('llm-token-limit is not available on Consumption; Basic v2 is the cheapest tier with every AI gateway policy.')
@allowed(['Developer', 'BasicV2', 'StandardV2'])
param apimSku string = 'BasicV2'

param tenantTokensPerMinute int = 20000
@description('Tokens per tenant subscription per calendar month (llm-token-limit token-quota); past it the gateway answers 403 until the month turns.')
param tenantTokensPerMonth int = 6000000

@description('Outbound from the apps limited to Azure service tags on 443 (network.bicep); false leaves the default outbound open.')
param egressControl bool = true

@description('The only sizes an Azure ML compute or managed online deployment may use (Azure Policy, deny).')
param allowedMlSizes array = ['Standard_F2s_v2', 'Standard_F4s_v2', 'Standard_DS2_v2', 'Standard_DS3_v2']
param maxDeploymentInstances int = 2

@description('Deploy the EU Foundry resource for EU accounts (modules/foundry-eu.bicep). Confirm in the delivery week that the EU models offer DataZoneStandard in `euLocation`.')
param euFoundry bool = true
@description('An EU data zone region with Mistral-Large-3 on Data Zone Standard.')
param euLocation string = 'swedencentral'
@description('The EU deployments: nw/config.py EU_MODELS names Mistral-Large-3 for Workhorse and Economy; there is no EU Judge.')
param euModels array = [
  {
    role: 'workhorse'
    name: 'Mistral-Large-3'
    format: 'Mistral AI'
    version: '1'
    sku: 'DataZoneStandard'
    capacity: 100
  }
]

@description('A second Foundry resource (https://<name>.services.ai.azure.com) that joins the gateway pool at priority 2.')
param secondaryFoundryEndpoint string = ''

@description('Empty picks basic for up to 15 owners (Basic holds 15 indexes) and standard above.')
@allowed(['', 'basic', 'standard'])
param searchSku string = ''

@allowed(['Basic', 'Standard'])
param acrSku string = 'Basic'

param purview bool = false
param defenderForContainers bool = false
param retrainEnabled bool = false
param retrainCron string = '0 6 * * 1'
param latencyP95Ms int = 2000

@description('The three model roles (ADR 0010), verified against the Foundry catalogue on 2026-09-29; the deployment name is the model id nw/config.py sends.')
param models array = [
  {
    role: 'workhorse'
    name: 'gpt-oss-120b'
    format: 'OpenAI-OSS'
    version: '1'
    sku: 'GlobalStandard'
    capacity: 100
  }
  {
    // Cheaper than the Workhorse on input and output (ADR 0013); sold through Azure Marketplace.
    role: 'economy'
    name: 'mistral-small-2503'
    format: 'Mistral AI'
    version: '1'
    sku: 'GlobalStandard'
    capacity: 100
  }
  {
    role: 'judge'
    name: 'claude-opus-5'
    format: 'Anthropic'
    version: '2'
    sku: 'GlobalStandard'
    capacity: 20
  }
  {
    role: 'embedding'
    name: 'text-embedding-3-small'
    format: 'OpenAI'
    version: '1'
    sku: 'GlobalStandard'
    capacity: 150
  }
]

@description('Five hex characters of SHA-256("<subscription>/<resource group>/<environment>"), the scope nw/platform/azure.py puts in endpoint names; the deploy script computes it.')
@minLength(5)
@maxLength(5)
param endpointScope string

@description('Claude is sold through Azure Marketplace; Foundry asks for the buyer (industry in lowercase).')
param claudeOrganizationName string = 'Northwind course'
param claudeCountryCode string = 'US'
param claudeIndustry string = 'technology'

@description('Allow API keys on the Foundry resource; off means Entra ID only.')
param foundryLocalAuth bool = false

@description('The platform owner key (instructor tooling); the deploy script keeps it in Key Vault across deploys.')
@secure()
param apiKey string
@description('Owner to its own x-api-key: every owner\'s apps accept only their owner\'s key. The deploy script keeps them in Key Vault across deploys.')
@secure()
param apiKeys object = {}

@secure()
param litellmMasterKey string = ''
@secure()
param postgresPassword string = ''
@secure()
param litellmSaltKey string = ''
@description('LiteLLM mode: owner to virtual key, kept in Key Vault across deploys by the deploy script.')
@secure()
param gatewayKeys object = {}

@description('Endpoint name to traffic, captured by the deploy script so a redeploy keeps the drill split.')
param endpointTraffic object = {}
@description('Live app name to {image, traffic}, captured by the deploy script so a redeploy keeps the release.')
param liveApps object = {}

@description('owner/name: the deployer trusts GitHub Actions OIDC for the environments `<environment>-canary` and `-live`, the builder trusts main.')
param githubRepository string = ''
param azureDevOpsIssuer string = ''
@description('Subject of the Azure Pipelines service connection bound to the deployer.')
param azureDevOpsSubject string = ''
@description('Subject of the Azure Pipelines service connection bound to the builder (image pushes only).')
param azureDevOpsBuilderSubject string = ''

@description('Entra object id of the platform owner (the deploy script passes the signed-in user): data plane roles for the uploads, indexes and keys.')
param adminObjectId string = ''
@allowed(['User', 'ServicePrincipal', 'Group'])
param adminPrincipalType string = 'User'

@description('Azure Verified Modules usage telemetry; off in the course.')
param enableTelemetry bool = false

var tenantList = mode == 'solo' ? ['solo'] : tenants
var embeddingDeployment = first(filter(models, m => m.role == 'embedding')).name
var endpointKinds = ['triage', 'semantic']
var owners = concat(tenantList, ['live'])
var suffix = take(uniqueString(resourceGroup().id, environment), 6)
var tags = {
  environment: environment
  course: 'ai-engineering'
  mode: mode
}
var liveEndpointNames = [for k in endpointKinds: 'nw-live-${k}-${endpointScope}']
var effectiveSearchSku = !empty(searchSku) ? searchSku : (length(owners) > 15 ? 'standard' : 'basic')

module observability 'modules/observability.bicep' = {
  name: 'observability'
  params: {
    environment: environment
    location: location
    tags: tags
    alertEmail: alertEmail
    budgetUsd: budgetUsd
    budgetStartDate: budgetStartDate
    enableTelemetry: enableTelemetry
  }
}

module network 'modules/network.bicep' = {
  name: 'network'
  params: {
    environment: environment
    location: location
    tags: tags
    egressControl: egressControl
    postgres: gatewayKind == 'litellm'
  }
}

module policy 'modules/policy.bicep' = {
  name: '${environment}-policy'
  scope: subscription()
  params: {
    environment: environment
    resourceGroupName: resourceGroup().name
  }
}

// Assigned to this resource group only: no tenant (and no one else) starts a compute or an
// online deployment outside the list.
resource sizesAssignment 'Microsoft.Authorization/policyAssignments@2025-01-01' = {
  name: '${environment}-ml-sizes'
  properties: {
    displayName: '${environment}: allowed Azure ML sizes'
    policyDefinitionId: policy.outputs.definitionId
    enforcementMode: 'Default'
    parameters: {
      allowedSizes: {
        value: allowedMlSizes
      }
      maxInstances: {
        value: maxDeploymentInstances
      }
    }
    nonComplianceMessages: [
      {
        message: 'Northwind platform: Azure ML computes and online deployments use ${join(allowedMlSizes, ', ')} with at most ${maxDeploymentInstances} instances.'
      }
    ]
  }
}

module data 'modules/data.bicep' = {
  name: 'data'
  params: {
    environment: environment
    location: location
    tags: tags
    suffix: suffix
    purview: purview
    owners: owners
    logsWorkspaceId: observability.outputs.logsWorkspaceId
    enableTelemetry: enableTelemetry
  }
}

module tracking 'modules/tracking.bicep' = {
  name: 'tracking'
  params: {
    environment: environment
    location: location
    tags: tags
    suffix: suffix
    tenants: owners
    acrSku: acrSku
    lakeName: data.outputs.lakeName
    lakeId: data.outputs.lakeId
    workspaceStorageId: data.outputs.workspaceStorageId
    appInsightsId: observability.outputs.appInsightsId
    logsWorkspaceId: observability.outputs.logsWorkspaceId
    // The registry is created in this module, so the image name is built from its known name.
    pipelinesImage: empty(imageTag)
      ? 'mcr.microsoft.com/azureml/openmpi4.1.0-ubuntu22.04:latest'
      : 'nwacr${suffix}.azurecr.io/nw-pipelines:${imageTag}'
    pipelinesImageVersion: empty(imageTag) ? '0' : imageTag
    workspaceStorageName: data.outputs.workspaceStorageName
    apiKey: apiKey
    apiKeys: apiKeys
    appInsightsConnectionString: observability.outputs.appInsightsConnectionString
    enableTelemetry: enableTelemetry
  }
}

module retrieval 'modules/retrieval.bicep' = {
  name: 'retrieval'
  params: {
    environment: environment
    location: location
    tags: tags
    suffix: suffix
    sku: effectiveSearchSku
    logsWorkspaceId: observability.outputs.logsWorkspaceId
    enableTelemetry: enableTelemetry
  }
}

module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  params: {
    environment: environment
    location: location
    tags: tags
    suffix: suffix
    models: models
    claudeOrganizationName: claudeOrganizationName
    claudeCountryCode: claudeCountryCode
    claudeIndustry: claudeIndustry
    localAuth: foundryLocalAuth
    appInsightsId: observability.outputs.appInsightsId
    appInsightsConnectionString: observability.outputs.appInsightsConnectionString
    searchPrincipalId: retrieval.outputs.principalId
    searchEndpoint: retrieval.outputs.endpoint
    searchName: retrieval.outputs.name
    acrName: tracking.outputs.acrName
    logsWorkspaceId: observability.outputs.logsWorkspaceId
    enableTelemetry: enableTelemetry
  }
}

// Every principal that calls the EU resource directly: the owners' identities, the learners
// named in tenantUsers, and the LiteLLM gateway's identity in LiteLLM mode.
var euPrincipals = concat(
  map(range(0, length(owners)), i => { id: tracking.outputs.identityPrincipalIds[i], type: 'ServicePrincipal' }),
  map(filter(items(tenantUsers), u => contains(tenantList, u.key)), u => { id: u.value, type: 'User' }),
  gatewayKind == 'litellm' ? [{ id: gateway.outputs.litellmPrincipalId, type: 'ServicePrincipal' }] : []
)

module foundryEu 'modules/foundry-eu.bicep' = if (euFoundry) {
  name: 'foundry-eu'
  params: {
    environment: environment
    location: euLocation
    tags: tags
    suffix: suffix
    models: euModels
    contentFilters: foundry.outputs.contentFilters
    principals: euPrincipals
    logsWorkspaceId: observability.outputs.logsWorkspaceId
    enableTelemetry: enableTelemetry
  }
}

var euEndpoint = euFoundry ? 'https://${environment}-foundry-eu-${suffix}.services.ai.azure.com' : ''

module admin 'modules/admin.bicep' = {
  name: 'admin'
  params: {
    environment: environment
    adminObjectId: adminObjectId
    adminPrincipalType: adminPrincipalType
    lakeName: data.outputs.lakeName
    searchName: retrieval.outputs.name
    keyVaultName: tracking.outputs.keyVaultName
    foundryAccountName: foundry.outputs.accountName
    workspaceName: tracking.outputs.workspaceName
    liveEndpointNames: liveEndpointNames
    endpointRoleId: tracking.outputs.tenantEndpointRoleId
  }
  // The live endpoints the instructor's role is assigned on.
  dependsOn: [liveOwner]
}

// One Container Apps environment (consumption) for every app: the owners' services and, in
// LiteLLM mode, the gateway. Logs go to the platform's Log Analytics workspace.
module appsEnvironment 'br/public:avm/res/app/managed-environment:0.16.0' = {
  name: 'apps-environment'
  params: {
    name: '${environment}-apps'
    location: location
    tags: tags
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsWorkspaceResourceId: observability.outputs.logsWorkspaceId
    }
    zoneRedundant: false
    publicNetworkAccess: 'Enabled'
    // In the platform's network: outbound through the apps subnet's security group, and the
    // LiteLLM database reachable privately (network.bicep). Ingress stays public (external).
    infrastructureSubnetResourceId: network.outputs.appsSubnetId
    internal: false
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    enableTelemetry: enableTelemetry
  }
}

module gateway 'modules/gateway.bicep' = {
  name: 'gateway'
  params: {
    environment: environment
    location: location
    tags: tags
    suffix: suffix
    kind: gatewayKind
    owners: owners
    apimSku: apimSku
    publisherEmail: empty(alertEmail) ? 'platform@example.com' : alertEmail
    tenantTokensPerMinute: tenantTokensPerMinute
    tenantTokensPerMonth: tenantTokensPerMonth
    foundryAccountName: foundry.outputs.accountName
    foundryEndpoint: foundry.outputs.endpoint
    secondaryFoundryEndpoint: secondaryFoundryEndpoint
    keyVaultName: tracking.outputs.keyVaultName
    appInsightsId: observability.outputs.appInsightsId
    appInsightsConnectionString: observability.outputs.appInsightsConnectionString
    containerAppsEnvironmentId: appsEnvironment.outputs.resourceId
    dbSubnetId: network.outputs.dbSubnetId
    dbZoneId: network.outputs.dbZoneId
    models: models
    euFoundryEndpoint: euEndpoint
    euModels: euModels
    litellmMasterKey: litellmMasterKey
    postgresPassword: postgresPassword
    litellmSaltKey: litellmSaltKey
    gatewayKeys: gatewayKeys
    enableTelemetry: enableTelemetry
  }
}

// What every job and app is told about the platform (the outputs.json keys). The Application
// Insights connection string is not here: the apps read it from Key Vault by reference
// (agents.bicep), and a pipeline job, which has no Key Vault reference, does without it.
var platformSettings = {
  NW_TRACK: 'azure'
  NW_ENVIRONMENT: environment
  NW_LOG_FORMAT: 'json'
  NW_AZURE_SUBSCRIPTION_ID: subscription().subscriptionId
  NW_AZURE_RESOURCE_GROUP: resourceGroup().name
  NW_AZURE_LOCATION: location
  NW_AZURE_ML_WORKSPACE: tracking.outputs.workspaceName
  NW_AZURE_FOUNDRY_ENDPOINT: foundry.outputs.endpoint
  NW_AZURE_FOUNDRY_PROJECT: foundry.outputs.projectName
  NW_AZURE_FOUNDRY_EU_ENDPOINT: euEndpoint
  // Behind the Container Apps ingress: the client address is the first forwarded hop.
  NW_TRUSTED_PROXY_HOPS: '1'
  NW_AZURE_SEARCH_ENDPOINT: retrieval.outputs.endpoint
  NW_AZURE_KEY_VAULT: tracking.outputs.keyVaultName
  NW_AZURE_ACR: tracking.outputs.acrName
  NW_AZURE_STORAGE_ACCOUNT: data.outputs.lakeName
  NW_AZURE_APIM_GATEWAY_URL: gateway.outputs.apimGatewayUrl
  NW_AZURE_CONTAINERAPPS_ENV: appsEnvironment.outputs.name
  NW_AZURE_ARTIFACTS_CONTAINER: 'artifacts'
  NW_AZURE_EMBEDDING_DEPLOYMENT: embeddingDeployment
  NW_AZURE_EMBEDDING_DIMENSIONS: '1536'
  NW_AZURE_RAI_POLICY: foundry.outputs.raiPolicyName
}

// `live` first: the tenants' roles on the live endpoint need it to exist.
module liveOwner 'modules/tenant.bicep' = {
  name: 'owner-live'
  params: {
    environment: environment
    location: location
    tags: tags
    owner: 'live'
    principalId: tracking.outputs.identityPrincipalIds[length(owners) - 1]
    identityId: tracking.outputs.identityIds[length(owners) - 1]
    identityClientId: tracking.outputs.identityClientIds[length(owners) - 1]
    userObjectId: ''
    lakeName: data.outputs.lakeName
    workspaceStorageName: data.outputs.workspaceStorageName
    keyVaultName: tracking.outputs.keyVaultName
    acrName: tracking.outputs.acrName
    workspaceName: tracking.outputs.workspaceName
    tenantWorkspaceRoleId: tracking.outputs.tenantWorkspaceRoleId
    tenantEndpointRoleId: tracking.outputs.tenantEndpointRoleId
    tenantProjectRoleId: foundry.outputs.tenantProjectRoleId
    endpointKinds: endpointKinds
    endpointScope: endpointScope
    foundryAccountName: foundry.outputs.accountName
    foundryProjectName: foundry.outputs.projectName
    clusterId: tracking.outputs.clusterId
    pipelinesEnvironmentId: tracking.outputs.pipelinesEnvironmentId
    retrainEnabled: false
    retrainCron: retrainCron
    endpointTraffic: endpointTraffic
    jobEnv: platformSettings
  }
}

module tenantOwners 'modules/tenant.bicep' = [
  for (t, i) in tenantList: {
    name: 'owner-${t}'
    params: {
      environment: environment
      location: location
      tags: tags
      owner: t
      principalId: tracking.outputs.identityPrincipalIds[i]
      identityId: tracking.outputs.identityIds[i]
      identityClientId: tracking.outputs.identityClientIds[i]
      userObjectId: tenantUsers[?t] ?? ''
      lakeName: data.outputs.lakeName
      workspaceStorageName: data.outputs.workspaceStorageName
      keyVaultName: tracking.outputs.keyVaultName
      acrName: tracking.outputs.acrName
      workspaceName: tracking.outputs.workspaceName
      tenantWorkspaceRoleId: tracking.outputs.tenantWorkspaceRoleId
      tenantEndpointRoleId: tracking.outputs.tenantEndpointRoleId
      tenantProjectRoleId: foundry.outputs.tenantProjectRoleId
      endpointKinds: endpointKinds
      endpointScope: endpointScope
      foundryAccountName: foundry.outputs.accountName
      foundryProjectName: foundry.outputs.projectName
      clusterId: tracking.outputs.clusterId
      pipelinesEnvironmentId: tracking.outputs.pipelinesEnvironmentId
      retrainEnabled: retrainEnabled
      retrainCron: retrainCron
      endpointTraffic: endpointTraffic
      jobEnv: platformSettings
    }
    // Tenants and live are deployed one after the other, as before.
    dependsOn: [liveOwner]
  }
]

module agents 'modules/agents.bicep' = {
  name: 'agents'
  params: {
    environment: environment
    location: location
    tags: tags
    owners: owners
    environmentId: appsEnvironment.outputs.resourceId
    defaultDomain: appsEnvironment.outputs.defaultDomain
    acrLoginServer: tracking.outputs.acrLoginServer
    imageTag: imageTag
    keyVaultUri: tracking.outputs.keyVaultUri
    identityIds: tracking.outputs.identityIds
    identityClientIds: tracking.outputs.identityClientIds
    litellmUrl: gateway.outputs.litellmUrl
    platformSettings: platformSettings
    liveApps: liveApps
    opsStore: data.outputs.opsStore
    enableTelemetry: enableTelemetry
  }
  // The identities need AcrPull and Key Vault Secrets User before a revision can start.
  dependsOn: [liveOwner, tenantOwners]
}

module serving 'modules/serving.bicep' = {
  name: 'serving'
  params: {
    environment: environment
    tags: tags
    liveEndpoints: [
      for (k, i) in endpointKinds: {
        kind: k
        id: resourceId(
          'Microsoft.MachineLearningServices/workspaces/onlineEndpoints',
          tracking.outputs.workspaceName,
          liveOwner.outputs.endpointNames[i]
        )
      }
    ]
    liveAppIds: [
      for kind in ['policy', 'agent', 'mcp']: {
        name: '${environment}-live-${kind}'
        id: resourceId('Microsoft.App/containerApps', '${environment}-live-${kind}')
      }
    ]
    actionGroupId: observability.outputs.actionGroupId
    latencyP95Ms: latencyP95Ms
    enableTelemetry: enableTelemetry
  }
  dependsOn: [agents]
}

module delivery 'modules/delivery.bicep' = {
  name: 'delivery'
  params: {
    environment: environment
    location: location
    tags: tags
    acrName: tracking.outputs.acrName
    githubRepository: githubRepository
    azureDevOpsIssuer: azureDevOpsIssuer
    azureDevOpsSubject: azureDevOpsSubject
    azureDevOpsBuilderSubject: azureDevOpsBuilderSubject
    liveAppNames: ['${environment}-live-policy', '${environment}-live-agent']
    liveIdentityName: '${environment}-live-id'
    appsEnvironmentName: appsEnvironment.outputs.name
    workspaceName: tracking.outputs.workspaceName
    liveEndpointNames: liveEndpointNames
    endpointRoleId: tracking.outputs.tenantEndpointRoleId
    keyVaultName: tracking.outputs.keyVaultName
    enableTelemetry: enableTelemetry
  }
  // The live apps and endpoints the deployer's roles are assigned on.
  dependsOn: [agents, liveOwner]
}

module defender 'modules/defender.bicep' = if (defenderForContainers) {
  name: '${environment}-defender-containers'
  scope: subscription()
}

// ----- outputs: scripts/deploy_azure.sh writes them to deploy/azure/outputs.json ------------------
// The first thirteen are the contract with nw/platform/azure.py.

output NW_AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output NW_AZURE_RESOURCE_GROUP string = resourceGroup().name
output NW_AZURE_LOCATION string = location
output NW_AZURE_ML_WORKSPACE string = tracking.outputs.workspaceName
output NW_AZURE_FOUNDRY_ENDPOINT string = foundry.outputs.endpoint
output NW_AZURE_FOUNDRY_PROJECT string = foundry.outputs.projectName
output NW_AZURE_SEARCH_ENDPOINT string = retrieval.outputs.endpoint
output NW_AZURE_KEY_VAULT string = tracking.outputs.keyVaultName
output NW_AZURE_ACR string = tracking.outputs.acrName
output NW_AZURE_STORAGE_ACCOUNT string = data.outputs.lakeName
output NW_AZURE_APPINSIGHTS_CONNECTION_STRING string = observability.outputs.appInsightsConnectionString
output NW_AZURE_APIM_GATEWAY_URL string = gateway.outputs.apimGatewayUrl
output NW_AZURE_CONTAINERAPPS_ENV string = appsEnvironment.outputs.name

// Beyond the contract: what the scripts and the reference tab use.
output NW_ENVIRONMENT string = environment
output NW_MODE string = mode
output NW_AZURE_NAME_SUFFIX string = suffix
// LiteLLM only (gateway kind litellm), empty behind API Management: NW_GATEWAY_URL is the
// LiteLLM route in nw.llm.providers, and APIM's URL is NW_AZURE_APIM_GATEWAY_URL above.
output NW_GATEWAY_URL string = gateway.outputs.litellmUrl
output NW_AZURE_CONTENT_SAFETY_ENDPOINT string = foundry.outputs.contentSafetyEndpoint
output NW_AZURE_GATEWAY_KIND string = gatewayKind
output NW_AZURE_ACR_LOGIN_SERVER string = tracking.outputs.acrLoginServer
output NW_AZURE_KEY_VAULT_URI string = tracking.outputs.keyVaultUri
output NW_AZURE_FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output NW_AZURE_ML_COMPUTE string = tracking.outputs.clusterName
output NW_AZURE_LIVE_ENDPOINT string = liveOwner.outputs.endpointNames[0]
output NW_AZURE_ENDPOINT_SCOPE string = endpointScope
output NW_AZURE_ARTIFACTS_CONTAINER string = 'artifacts'
// Durable ops state (nw/agent/opstore.py): the apps get it from agents.bicep; a hosted agent
// gets it from nw/platform/azure.py, which reads this output.
output NW_OPS_STORE string = data.outputs.opsStore
output NW_AZURE_DATASTORE string = 'workspaceblobstore'
output NW_AZURE_EMBEDDING_DEPLOYMENT string = embeddingDeployment
output NW_AZURE_EMBEDDING_DIMENSIONS string = '1536'
output NW_AZURE_RAI_POLICY string = foundry.outputs.raiPolicyName
output NW_AZURE_ENDPOINT_SKU string = 'Standard_F2s_v2'
output NW_AZURE_LIVE_ENDPOINT_SKU string = 'Standard_DS3_v2'
@description('Solo mode: the one tenant\'s identity, which pipeline jobs run as. Cohort: per tenant in NW_AZURE_TENANTS.')
output NW_AZURE_PIPELINE_IDENTITY_CLIENT_ID string = mode == 'solo' ? tracking.outputs.identityClientIds[0] : ''
output NW_AZURE_SEARCH_SERVICE string = retrieval.outputs.name
output NW_AZURE_APIM_NAME string = gateway.outputs.apimName
output NW_AZURE_DEPLOYER_CLIENT_ID string = delivery.outputs.deployerClientId
output NW_AZURE_FOUNDRY_EU_ENDPOINT string = euEndpoint
output NW_AZURE_BUILDER_CLIENT_ID string = delivery.outputs.builderClientId
output NW_AZURE_SIGNING_KEY string = delivery.outputs.signingKeyName
output NW_AZURE_EGRESS_CONTROL bool = egressControl
output NW_AZURE_WORKBOOK string = observability.outputs.workbookName
output NW_AZURE_LAKE_CONTAINERS array = data.outputs.containers
output NW_AZURE_OWNERS array = owners
output NW_AZURE_TENANTS array = [
  for (t, i) in tenantList: {
    tenant: t
    prefix: '${environment}-${t}'
    identity_client_id: tracking.outputs.identityClientIds[i]
    endpoints: tenantOwners[i].outputs.endpointNames
    retrain_schedule: tenantOwners[i].outputs.scheduleName
    search_index: '${environment}-${t}-policies'
    gateway_key_secret: '${environment}-${t}-gateway-key'
    api_key_secret: '${environment}-${t}-api-key'
    artifacts_prefix: 'abfss://artifacts@${data.outputs.lakeName}.dfs.${az.environment().suffixes.storage}/${environment}-${t}'
    pipelines_prefix: 'abfss://pipelines@${data.outputs.lakeName}.dfs.${az.environment().suffixes.storage}/${environment}-${t}'
  }
]
output NW_AZURE_APPS array = agents.outputs.apps
