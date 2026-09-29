// One course service on Container Apps: the image from the platform registry pulled with the
// owner's identity, the cohort API key and the owner's gateway key injected from Key Vault by
// reference, Application Insights for traces. `multipleRevisions` turns on the revision traffic
// split the delivery drill moves (live services only).
metadata owner = 'northwind'

param name string
param location string
param tags object
param environmentId string
param identityId string
param identityClientId string
param image string
param registryServer string
param port int
param external bool
param env array
param secrets array
param multipleRevisions bool
param traffic array
param minReplicas int
param cpu string
param memory string
param enableTelemetry bool

// Before `make images-azure` the app runs Microsoft's quickstart image, which listens on 80.
var placeholder = !startsWith(image, registryServer)
var targetPort = placeholder ? 80 : port
// The MCP server answers only MCP requests, so its probe is TCP; the services have /readyz.
var probe = port == 8020
  ? { tcpSocket: { port: targetPort } }
  : { httpGet: { path: '/readyz', port: targetPort } }

module app 'br/public:avm/res/app/container-app:0.23.0' = {
  name: name
  params: {
    name: name
    location: location
    tags: tags
    environmentResourceId: environmentId
    managedIdentities: {
      userAssignedResourceIds: [identityId]
    }
    // The placeholder image (before `make images-azure`) is public; the course images come from
    // the platform registry with the owner's AcrPull.
    registries: !placeholder
      ? [
          {
            server: registryServer
            identity: identityId
          }
        ]
      : []
    ingressExternal: external
    ingressTargetPort: targetPort
    ingressAllowInsecure: false
    ingressTransport: 'auto'
    activeRevisionsMode: multipleRevisions ? 'Multiple' : 'Single'
    traffic: multipleRevisions ? traffic : null
    maxInactiveRevisions: 5
    scaleSettings: {
      minReplicas: minReplicas
      maxReplicas: 3
    }
    secrets: secrets
    containers: [
      {
        name: 'service'
        image: image
        resources: {
          cpu: json(cpu)
          memory: memory
        }
        env: concat(
          [
            { name: 'AZURE_CLIENT_ID', value: identityClientId }
            { name: 'PORT', value: string(targetPort) }
          ],
          env
        )
        probes: placeholder
          ? []
          : [
              union(
                {
                  type: 'Readiness'
                  initialDelaySeconds: 10
                  periodSeconds: 15
                  failureThreshold: 8
                },
                probe
              )
            ]
      }
    ]
    enableTelemetry: enableTelemetry
  }
}

output name string = app.outputs.name
output fqdn string = app.outputs.fqdn
output id string = app.outputs.resourceId
