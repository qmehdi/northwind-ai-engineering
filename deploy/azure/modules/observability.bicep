// Identity and observability, part one: where every other area sends its telemetry.
// Log Analytics and workspace-based Application Insights (Azure Verified Modules), the action
// group every alert notifies, the budget on the resource group, the platform workbook, and
// the `drift_alert` log alert that every service's drift check feeds (nw/serving/drift.py
// logs the line; CDK and Terraform alarm on it the same way).
metadata owner = 'northwind'

param environment string
param location string
param tags object
param alertEmail string
param budgetUsd int
param budgetStartDate string
param enableTelemetry bool

resource logsWorkspace 'Microsoft.OperationalInsights/workspaces@2026-03-01' existing = {
  name: '${environment}-logs'
  dependsOn: [logs]
}

module logs 'br/public:avm/res/operational-insights/workspace:0.16.1' = {
  name: '${environment}-logs'
  params: {
    name: '${environment}-logs'
    location: location
    tags: tags
    skuName: 'PerGB2018'
    dataRetention: 30
    // A runaway loop cannot ingest more than this a day; 1 GB is about 2.76 USD.
    dailyQuotaGb: '1'
    enableTelemetry: enableTelemetry
  }
}

module appInsights 'br/public:avm/res/insights/component:0.8.0' = {
  name: '${environment}-appinsights'
  params: {
    name: '${environment}-appinsights'
    location: location
    tags: tags
    workspaceResourceId: logs.outputs.resourceId
    applicationType: 'web'
    retentionInDays: 30
    enableTelemetry: enableTelemetry
  }
}

module actionGroup 'br/public:avm/res/insights/action-group:0.8.0' = {
  name: '${environment}-alerts'
  params: {
    name: '${environment}-alerts'
    groupShortName: take(replace(environment, '-', ''), 12)
    location: 'global'
    tags: tags
    emailReceivers: empty(alertEmail)
      ? []
      : [
          {
            name: 'platform-owner'
            emailAddress: alertEmail
            useCommonAlertSchema: true
          }
        ]
    enableTelemetry: enableTelemetry
  }
}

// Budgets have no Azure Verified Module at resource group scope; the resource is native.
resource budget 'Microsoft.Consumption/budgets@2026-06-01' = {
  name: '${environment}-budget'
  properties: {
    category: 'Cost'
    amount: budgetUsd
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    notifications: {
      actual50: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: empty(alertEmail) ? [] : [alertEmail]
        contactGroups: [actionGroup.outputs.resourceId]
      }
      actual90: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 90
        thresholdType: 'Actual'
        contactEmails: empty(alertEmail) ? [] : [alertEmail]
        contactGroups: [actionGroup.outputs.resourceId]
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: empty(alertEmail) ? [] : [alertEmail]
        contactGroups: [actionGroup.outputs.resourceId]
      }
    }
  }
}

// Every service logs `drift_alert` when PSI crosses its bar; one log search alert across the
// Container Apps console logs and Application Insights traces, evaluated every 15 minutes.
module driftAlert 'br/public:avm/res/insights/scheduled-query-rule:0.6.0' = {
  name: '${environment}-drift-alert'
  params: {
    name: '${environment}-drift-alert'
    alertDisplayName: '${environment}: drift_alert logged by a service'
    alertDescription: 'A service logged drift_alert: PSI against the training profile crossed the bar. Read /drift on the service named in the log line.'
    location: location
    tags: tags
    kind: 'LogAlert'
    severity: 2
    evaluationFrequency: 'PT15M'
    windowSize: 'PT15M'
    scopes: [logs.outputs.resourceId]
    // The Container Apps table exists only after the first app logs; validation would fail on
    // a fresh workspace.
    skipQueryValidation: true
    autoMitigate: false
    criterias: {
      allOf: [
        {
          query: 'union isfuzzy=true (ContainerAppConsoleLogs_CL | project TimeGenerated, Line = tostring(Log_s)), (AppTraces | project TimeGenerated, Line = Message) | where Line has "drift_alert"'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroupResourceIds: [actionGroup.outputs.resourceId]
    }
    enableTelemetry: enableTelemetry
  }
}

// The platform workbook: requests and failures per service, tokens per tenant from the model
// gateway's metric, the live endpoint, pipeline runs and drift lines. Workbooks have no
// Azure Verified Module; the name must be a GUID.
var workbookItems = [
  {
    type: 1
    content: {
      json: '## ${environment} platform\nRequests per service, model tokens per tenant, the live endpoint and drift. Every tile reads the one Log Analytics workspace.'
    }
    name: 'title'
  }
  {
    type: 3
    content: {
      version: 'KqlItem/1.0'
      query: 'AppRequests | summarize requests = count(), failures = countif(Success == false), p95_ms = percentile(DurationMs, 95) by AppRoleName | order by requests desc'
      size: 0
      title: 'Requests, failures and p95 per service (Application Insights)'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      visualization: 'table'
    }
    name: 'services'
  }
  {
    type: 3
    content: {
      version: 'KqlItem/1.0'
      query: 'AppMetrics | where Name in ("Total Tokens", "Prompt Tokens", "Completion Tokens") | extend subscription = tostring(Properties["Subscription ID"]) | summarize tokens = sum(Sum) by subscription, Name, bin(TimeGenerated, 1h) | order by TimeGenerated desc'
      size: 0
      title: 'Model tokens per tenant subscription (API Management llm-emit-token-metric)'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      visualization: 'barchart'
    }
    name: 'tokens'
  }
  {
    type: 3
    content: {
      version: 'KqlItem/1.0'
      query: 'AzureMetrics | where ResourceProvider == "MICROSOFT.MACHINELEARNINGSERVICES" and MetricName in ("RequestsPerMinute", "RequestLatency_P95") | summarize value = avg(Average) by Resource, MetricName, bin(TimeGenerated, 5m)'
      size: 0
      title: 'Online endpoints: requests and p95 latency'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      visualization: 'timechart'
    }
    name: 'endpoints'
  }
  {
    type: 3
    content: {
      version: 'KqlItem/1.0'
      query: 'union isfuzzy=true (ContainerAppConsoleLogs_CL | project TimeGenerated, App = ContainerAppName_s, Line = tostring(Log_s)), (AppTraces | project TimeGenerated, App = AppRoleName, Line = Message) | where Line has "drift_alert" | order by TimeGenerated desc | take 50'
      size: 0
      title: 'drift_alert lines'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      visualization: 'table'
    }
    name: 'drift'
  }
]

resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, environment, 'platform-workbook')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: '${environment} platform'
    category: 'workbook'
    sourceId: logsWorkspace.id
    serializedData: string({
      version: 'Notebook/1.0'
      items: workbookItems
      isLocked: false
      fallbackResourceIds: [logsWorkspace.id]
    })
  }
}

output logsWorkspaceId string = logs.outputs.resourceId
output logsWorkspaceName string = logs.outputs.name
output logsCustomerId string = logs.outputs.logAnalyticsWorkspaceId
output appInsightsId string = appInsights.outputs.resourceId
output appInsightsName string = appInsights.outputs.name
output appInsightsConnectionString string = appInsights.outputs.connectionString
output actionGroupId string = actionGroup.outputs.resourceId
output workbookName string = workbook.properties.displayName
