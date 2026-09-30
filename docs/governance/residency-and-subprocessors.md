# Residency and subprocessors

Where each model call and each stored byte is processed, per track, and who processes it. Every
vendor fact below was read on the vendor's own page on 2026-09-30; the page's own date is given
where it shows one. Vendor terms change: re-read the pages in the delivery week and before any
real deployment. Not legal advice.

Why it matters here: 67 of 240 accounts are `region: eu`, 388 tickets are German, and
Northwind's customer policy (`data/policies/gdpr-data-residency.md`) promises EU processing for
EU accounts. The code routes an EU account to the EU model ids in `EU_MODELS` (`nw/config.py`,
`nw/llm/residency.py`) and fails rather than fall back when a role has no EU model.

## Summary per track and role

| Track | Role | Default (US) | EU option | EU routing in `EU_MODELS` |
| --- | --- | --- | --- | --- |
| AWS | Workhorse gpt-oss-120b | `openai.gpt-oss-120b-1:0` in us-east-1 | In-region in eu-central-1, eu-north-1, eu-south-1, eu-west-1, eu-west-2; no `eu.` profile [B1] | Same id, called in an EU region |
| AWS | Judge Claude Opus 5 | Profile only, no in-region option on bedrock-runtime [B2] | `eu.anthropic.claude-opus-5`, "Keeps data within EU regions" [B2] | `eu.anthropic.claude-opus-5` |
| AWS | Economy Nova Micro | `us.amazon.nova-micro-v1:0` (US geography) | `eu.amazon.nova-micro-v1:0` [B3] | `eu.amazon.nova-micro-v1:0` |
| AWS | Embeddings Titan v2 | us-east-1 | In-region in every eu-* region [B4] | Region of the stack |
| Google Cloud | Workhorse and Economy gpt-oss-120b | `gpt-oss-120b-maas`, global endpoint only, ML processing in the US multi-region [G1] | **None** | None: EU accounts fail closed on these roles |
| Google Cloud | Judge Claude Opus 5 | US multi-region or global | Europe multi-region (`eu`, EU member states only, not the UK or Switzerland) [G2, G3] | `claude-opus-5` at the `eu` location |
| Azure | Workhorse gpt-oss-120b | GlobalStandard: "may be processed in any Azure region" [A1] | **Not found**: no DataZone or regional Standard listing for it [A2] | `Mistral-Large-3` (EU availability not verified here) |
| Azure | Economy mistral-small-2503 | GlobalStandard only [A3] | None listed | `Mistral-Large-3` |
| Azure | Judge claude-opus-5 | Anthropic is the seller and an independent processor; Anthropic-hosted runs outside Azure [A4] | **None**: "Customer Data is processed by Anthropic in the United States" [A5] | None: EU accounts fail closed |
| Azure | Embeddings text-embedding-3-small | GlobalStandard | DataZone Standard in EU regions (francecentral, germanywestcentral, swedencentral, westeurope and others) [A2] | Deployment type in the Bicep |
| Local | Workhorse, Economy gpt-oss:20b | On the laptop (Ollama) | On the laptop | Same |
| Local | Judge | `fake-judge`, or Claude through the Anthropic API when `ANTHROPIC_API_KEY` is set | None: `inference_geo` is `global` or `us` only [N1] | Keep the fake judge for EU data |

Stored data stays where the platform is deployed on every cloud (S3, GCS, Blob in the stack's
region). The IaC defaults are US regions (`us-east-1`, `us-central1` with the Judge in the US,
`eastus2`), so an EU deployment is a region choice at deploy time, not a default.

## Processors and subprocessors

| Party | Role for Northwind | Training on prompts | Retention of prompts and outputs | Terms and subprocessors |
| --- | --- | --- | --- | --- |
| Amazon Web Services (Bedrock) | Processor | No: providers "will not use any inputs to or outputs from Amazon Bedrock to train" [B5] | Zero retention by default; Opus 5 not in the retention list; inputs and outputs "not shared with any model providers" [B5, B6] | AWS GDPR DPA with the 2021 SCCs [B7]; subprocessors page updated 2026-07-28, 30 days' notice [B8] |
| Google Cloud (Agent Platform) | Processor | No, without prior permission [G4] | Zero retention needs an abuse-monitoring exception and request-response logging off [G4] | Cloud DPA, modified 2026-06-08 [G5]; subprocessors, modified 2026-08-20 [G6]. Claude is a partner model under separate terms; Anthropic is not on Google's subprocessor list [G7] |
| Microsoft (Foundry, Azure OpenAI models) | Processor | No; prompts "NOT available to OpenAI" [A6] | Stateless models; EEA abuse reviewers for EEA deployments [A6] | Products and Services DPA, 2026-05-22 [A7]; subprocessor list on the Service Trust Portal [A8] |
| Anthropic via Foundry | Independent processor under Anthropic's terms [A4] | Anthropic's commercial terms: no by default [N3] | Anthropic's terms | Anthropic DPA [N4] |
| Anthropic API (Local Judge) | Processor | No by default [N3] | Two current Anthropic pages disagree: deleted within 30 days [N2a], or not retained by default [N2b]; zero retention by arrangement | DPA with SCCs incorporated into the Commercial Terms [N4]; subprocessors in the Trust Center (not verified) |

## EU AI Act dates (for the catalog's `eu_ai_act_class`)

Article 50 transparency obligations apply from 2 August 2026. The Digital Omnibus on AI was
adopted (Parliament 16 June, Council 29 June, signed 8 July 2026): stand-alone Annex III
high-risk obligations move to 2 December 2027, high-risk in Annex I products to 2 August 2028,
and Article 50 content marking to 2 December 2026 for systems placed on the market before
2 August 2026 [E1]. The Official Journal reference was not verified.

## What an EU deployment needs, per track

- **AWS**: deploy in an EU region (eu-central-1); Workhorse and embeddings in-region; Judge and
  Economy through `eu.` profiles; the Bedrock IAM policy limited to EU regions.
- **Google Cloud**: the Judge at the `eu` location; no EU Workhorse today, so EU tickets need
  another model (a self-deployed open model in an EU region) or stay on another track.
- **Azure**: DataZone Standard (EU) deployments where offered; gpt-oss-120b and Claude have none,
  so EU tickets need an EU-available Workhorse and no Claude Judge.
- **Local**: everything on the laptop except an Anthropic Judge, which is US or global only.

## Sources (read 2026-09-30)

- [B1] https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-oss-120b.html
- [B2] https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-5.html
- [B3] https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-micro.html
- [B4] https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-titan-text-embeddings-v2.html
- [B5] https://aws.amazon.com/bedrock/faqs/
- [B6] https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html, https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html
- [B7] https://d1.awsstatic.com/legal/aws-gdpr/AWS_GDPR_DPA.pdf, https://aws.amazon.com/compliance/gdpr-center/
- [B8] https://aws.amazon.com/compliance/sub-processors/
- [G1] https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/openai/gpt-oss-120b (2026-09-28)
- [G2] https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/opus-5 (2026-09-28)
- [G3] https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/data-residency (2026-09-28)
- [G4] https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/zero-data-retention (2026-09-28)
- [G5] https://cloud.google.com/terms/data-processing-addendum
- [G6] https://cloud.google.com/terms/subprocessors
- [G7] https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude
- [A1] https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types (2026-08-12)
- [A2] https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure (2026-09-23) and its region-availability page (2026-09-04)
- [A3] https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-from-partners (2026-09-22)
- [A4] https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/claude-models/data-privacy (2026-06-29), https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models
- [A5] https://learn.microsoft.com/en-us/privacy/eudb/eu-data-boundary-transfers-for-all-services (2026-03-30)
- [A6] https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/openai/data-privacy (2026-06-05)
- [A7] https://www.microsoft.com/licensing/docs/view/Microsoft-Products-and-Services-Data-Protection-Addendum-DPA
- [A8] https://servicetrust.microsoft.com/DocumentPage/aead9e68-1190-4d90-ad93-36418de5c594
- [N1] https://platform.claude.com/docs/en/manage-claude/data-residency
- [N2a] https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data (2026-07-01)
- [N2b] https://platform.claude.com/docs/en/manage-claude/api-and-data-retention
- [N3] https://privacy.claude.com/en/articles/7996868-is-my-data-used-for-model-training (2026-08-18)
- [N4] https://www.anthropic.com/legal/data-processing-addendum
- [E1] https://www.europarl.europa.eu/legislative-train/package-digital-package/file-digital-omnibus-on-ai (2026-08-01)
