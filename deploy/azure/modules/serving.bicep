// Serving: the alerts the promotion drill watches. The online endpoints themselves are per
// owner (tenant.bicep): one per tenant for learning, one live endpoint whose blue and green
// deployments carry the canary. A canary is rolled back when one of these fires:
//
//   <environment>-live-5xx       server errors on the live triage endpoint (RequestsPerMinute,
//                                statusCodeClass 5xx); `-live-semantic-5xx` on the semantic one
//   <environment>-live-p95       p95 latency on the live triage endpoint above `latencyP95Ms`;
//                                `-live-semantic-p95` on the semantic one
//   <environment>-live-<app>-5xx server errors on the live policy and agent apps (the image canary)
//
// Metric names from the supported metrics pages of Microsoft.MachineLearningServices/workspaces/
// onlineEndpoints and Microsoft.App/containerApps (learn.microsoft.com, 2026-09-29).
metadata owner = 'northwind'

param environment string
param tags object
@description('Every live online endpoint: {kind, id}; triage keeps the names the guide uses.')
param liveEndpoints array
param liveAppIds array
param actionGroupId string
param latencyP95Ms int
param enableTelemetry bool

var endpointAlerts = [
  for e in liveEndpoints: {
    kind: e.kind
    id: e.id
    stem: e.kind == 'triage' ? '${environment}-live' : '${environment}-live-${e.kind}'
  }
]

module endpoint5xx 'br/public:avm/res/insights/metric-alert:0.4.1' = [
  for e in endpointAlerts: {
    name: '${e.stem}-5xx'
    params: {
      name: '${e.stem}-5xx'
      alertDescription: 'Server errors on the live ${e.kind} endpoint: roll the canary back (set traffic to the stable deployment).'
      location: 'global'
      tags: tags
      severity: 1
      evaluationFrequency: 'PT1M'
      windowSize: 'PT5M'
      scopes: [e.id]
      targetResourceType: 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints'
      autoMitigate: true
      criteria: {
        'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
        allof: [
          {
            name: 'server-errors'
            criterionType: 'StaticThresholdCriterion'
            metricNamespace: 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints'
            metricName: 'RequestsPerMinute'
            dimensions: [
              {
                name: 'statusCodeClass'
                operator: 'Include'
                values: ['5xx']
              }
            ]
            operator: 'GreaterThan'
            threshold: 0
            timeAggregation: 'Total'
          }
        ]
      }
      actions: [actionGroupId]
      enableTelemetry: enableTelemetry
    }
  }
]

module endpointP95 'br/public:avm/res/insights/metric-alert:0.4.1' = [
  for e in endpointAlerts: {
    name: '${e.stem}-p95'
    params: {
      name: '${e.stem}-p95'
      alertDescription: 'p95 latency on the live ${e.kind} endpoint above ${latencyP95Ms} ms: hold or roll back the canary.'
      location: 'global'
      tags: tags
      severity: 2
      evaluationFrequency: 'PT1M'
      windowSize: 'PT5M'
      scopes: [e.id]
      targetResourceType: 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints'
      autoMitigate: true
      criteria: {
        'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
        allof: [
          {
            name: 'p95-latency'
            criterionType: 'StaticThresholdCriterion'
            metricNamespace: 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints'
            metricName: 'RequestLatency_P95'
            operator: 'GreaterThan'
            threshold: latencyP95Ms
            timeAggregation: 'Average'
          }
        ]
      }
      actions: [actionGroupId]
      enableTelemetry: enableTelemetry
    }
  }
]

module app5xx 'br/public:avm/res/insights/metric-alert:0.4.1' = [
  for a in liveAppIds: {
    name: '${a.name}-5xx'
    params: {
      name: '${a.name}-5xx'
      alertDescription: 'Server errors on ${a.name}: `make approve-azure` must not run; the release script sets the stable revision back to 100.'
      location: 'global'
      tags: tags
      severity: 1
      evaluationFrequency: 'PT1M'
      windowSize: 'PT5M'
      scopes: [a.id]
      targetResourceType: 'Microsoft.App/containerApps'
      autoMitigate: true
      criteria: {
        'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
        allof: [
          {
            name: 'server-errors'
            criterionType: 'StaticThresholdCriterion'
            metricNamespace: 'Microsoft.App/containerApps'
            metricName: 'Requests'
            dimensions: [
              {
                name: 'statusCodeCategory'
                operator: 'Include'
                values: ['5xx']
              }
            ]
            operator: 'GreaterThan'
            threshold: 0
            timeAggregation: 'Total'
          }
        ]
      }
      actions: [actionGroupId]
      enableTelemetry: enableTelemetry
    }
  }
]

output alertNames array = concat(
  flatten(map(endpointAlerts, e => ['${e.stem}-5xx', '${e.stem}-p95'])),
  map(liveAppIds, a => '${a.name}-5xx')
)
