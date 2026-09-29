// Agents and the application layer on Container Apps. Per owner (every tenant, and `live`):
//
//   <environment>-<owner>-policy  the RAG inference service (Project 3), external ingress
//   <environment>-<owner>-agent   the agent runtime entrypoint (Project 4), external ingress
//   <environment>-<owner>-mcp     the MCP server with the course tools, internal ingress only:
//                                 reachable from the apps of the environment, never from outside
//
// The live agent runs here and not as a Foundry hosted agent (checked on learn.microsoft.com,
// 2026-09-29): "an agent endpoint serves one version at a time and routes 100% of its traffic
// to that version. Traffic splitting between versions isn't supported", so the canary of the
// promotion drill would have nowhere to run. Tenant agents run as hosted agents in the Foundry
// project (nw/platform/azure.py, FoundryHostedAgentRuntime); the tenants' apps here are the
// service layer. Both are registered in the registry document agents/agents.json, the live one
// with runtime `container-apps`. The Foundry Agent Service is in its basic setup (no capability
// host, platform-managed storage).
//
// The live policy and agent apps run in multiple-revision mode: `scripts/deploy_azure.sh
// release` adds a revision at 10 percent and `approve` moves it to 100 (the traffic split).
// `liveApps` carries what the drill set, so a redeploy of this template does not undo it.
metadata owner = 'northwind'

param environment string
param location string
param tags object
param owners array
param environmentId string
param defaultDomain string
param acrLoginServer string
param imageTag string
param keyVaultUri string
param identityIds array
param identityClientIds array
// The LiteLLM gateway's URL, empty behind API Management. APIM's URL reaches the apps as
// NW_AZURE_APIM_GATEWAY_URL through platformSettings; NW_GATEWAY_URL means LiteLLM only.
param litellmUrl string
param platformSettings object
param liveApps object
param enableTelemetry bool

var placeholderImage = 'mcr.microsoft.com/k8se/quickstart:latest'
var services = [
  { kind: 'policy', port: 8000, external: true, cpu: '1.0', memory: '2Gi', image: 'nw-policy' }
  { kind: 'agent', port: 8000, external: true, cpu: '1.0', memory: '2Gi', image: 'nw-agent' }
  { kind: 'mcp', port: 8020, external: false, cpu: '0.5', memory: '1Gi', image: 'nw-mcp' }
]
var apps = flatten(map(range(0, length(owners)), i => map(services, s => {
  owner: owners[i]
  index: i
  kind: s.kind
  name: '${environment}-${owners[i]}-${s.kind}'
  port: s.port
  external: s.external
  cpu: s.cpu
  memory: s.memory
  image: empty(imageTag) ? placeholderImage : '${acrLoginServer}/${s.image}:${imageTag}'
  live: owners[i] == 'live' && s.kind != 'mcp'
})))

module containerApps 'app.bicep' = [
  for a in apps: {
    name: '${a.name}-app'
    params: {
      name: a.name
      location: location
      tags: union(tags, { 'nw-tenant': a.owner, 'nw-service': a.kind })
      environmentId: environmentId
      identityId: identityIds[a.index]
      identityClientId: identityClientIds[a.index]
      image: a.live ? liveApps[?a.name].?image ?? a.image : a.image
      registryServer: acrLoginServer
      port: a.port
      external: a.external
      multipleRevisions: a.live
      traffic: a.live
        ? liveApps[?a.name].?traffic ?? [
            {
              latestRevision: true
              weight: 100
            }
          ]
        : []
      minReplicas: 0
      cpu: a.cpu
      memory: a.memory
      secrets: [
        {
          name: 'api-key'
          keyVaultUrl: '${keyVaultUri}secrets/${environment}-api-key'
          identity: identityIds[a.index]
        }
        {
          name: 'gateway-key'
          keyVaultUrl: '${keyVaultUri}secrets/${environment}-${a.owner}-gateway-key'
          identity: identityIds[a.index]
        }
      ]
      env: concat(
        map(items(platformSettings), s => { name: s.key, value: s.value }),
        [
          { name: 'NW_TENANT', value: a.owner }
          { name: 'OTEL_SERVICE_NAME', value: a.name }
          { name: 'NW_API_KEY', secretRef: 'api-key' }
          { name: 'NW_GATEWAY_KEY', secretRef: 'gateway-key' }
          { name: 'NW_POLICY_URL', value: 'http://${environment}-${a.owner}-policy' }
          { name: 'NW_MCP_URL', value: 'http://${environment}-${a.owner}-mcp/mcp' }
        ],
        empty(litellmUrl) ? [] : [{ name: 'NW_GATEWAY_URL', value: litellmUrl }],
        a.kind == 'mcp'
          ? [
              { name: 'NW_MCP_HOST', value: '0.0.0.0' }
              { name: 'NW_MCP_PORT', value: '8020' }
              {
                name: 'NW_MCP_ALLOWED_HOSTS'
                value: '${a.name}:*,${a.name}.internal.${defaultDomain}:*,localhost:*'
              }
            ]
          : []
      )
      enableTelemetry: enableTelemetry
    }
  }
]

output apps array = [
  for (a, i) in apps: {
    owner: a.owner
    kind: a.kind
    name: a.name
    url: a.external ? 'https://${containerApps[i].outputs.fqdn}' : 'http://${a.name}'
  }
]
