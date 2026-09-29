# Changelog

Notable changes to this repository. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the package follows [Semantic Versioning](https://semver.org/). The package version is the one in `pyproject.toml` and on every `x-nw-version` header. The API (`x-api-version`), the images (digest and `vX.Y.Z` tag) and the data (`data/MANIFEST.json`, CalVer) carry their own versions; see the README's Versioning section.

`make release VERSION=x.y.z` moves the Unreleased section under a version and tags it.

## [Unreleased]

### Added

- Azure: `NW_AZURE_APIM_GATEWAY_URL` is read from `deploy/azure/outputs.json` when unset; `NW_GATEWAY_URL` means LiteLLM only (the Bicep no longer sets it to the API Management URL); `platform.agents.deploy` gives hosted agents the APIM route and the Foundry endpoint.
- Azure: preflight prints `azure track`, `azure identity` (an Entra ID token for `https://ai.azure.com/.default`, or the key) and `az`; `route` lines say `gateway=yes` behind API Management.
- Azure: `AzurePromptShields` screener (Content Safety `text:shieldPrompt`, api-version 2024-09-01) when `NW_AZURE_CONTENT_SAFETY_ENDPOINT` is set; the endpoint is a Bicep output.
- Azure: the Azure Monitor trace exporter in `nw/telemetry.py` on the azure track with an Application Insights connection string; it degrades to no exporter while the OpenTelemetry pin keeps it from importing.
- API versioning: every route under `/v1`, the bare paths kept as deprecated aliases for one release, `x-api-version` and `x-nw-version` on every response, `api_version` and `nw_version` in `/version` (`nw/api.py`).
- OpenAPI snapshots in `docs/openapi/`; `scripts/openapi_snapshot.py --check` fails on a removed path, a removed or retyped field, or a new required field, and passes additive change.
- API key ids: `NW_API_KEY` accepts one secret or a JSON map of key id to secret, from the environment or the cloud secret; every request line carries `api_key_id`, never the secret; `nw_requests_by_key_total{key_id}`; unknown keys are rejected.
- In-service rate limiting (`nw/ratelimit.py`): a token bucket per key id, or per client address without a key, `NW_RATE_LIMIT_RPS` (10) and `NW_RATE_LIMIT_BURST` (30), 429 with `Retry-After`, `nw_rate_limited_total{key_id}`; probes and `/metrics` exempt.
- `data/MANIFEST.json` and `python -m nw.data_manifest --check`: sha256 and row counts for the tickets, the accounts, every policy document, the golden sets, the adversarial set and the judge calibration set, with a dataset version, the licence and the provenance line.
- `nw.__version__` read from the installed package metadata.
- Workflows: `image` (build the triage image, Trivy, Syft SBOM, cosign keyless signature on main when a registry is configured), `audit` (pip-audit over the lock, gitleaks over the history, the OpenAPI and data manifest checks; weekly and on pull requests), `release` (a GitHub release from this file on a `v*` tag, image tags for the commit).
- gitleaks in `.pre-commit-config.yaml`.
- `docs/SECURITY.md`, the threat model with the control for each threat, and `docs/adr/` with the five system decisions.
- `scripts/release.py`: `cut` bumps `pyproject.toml` and this file; `notes` prints a version's section.

### Changed

- Every capture file (`NW_TRIAGE_CAPTURE`, `NW_SEMANTIC_CAPTURE`, `NW_POLICY_CAPTURE`, `NW_AGENT_CAPTURE`) and the feedback log pass free text through `nw.policy.redact` before a line is written.
- Dockerfile base images pinned by digest: `python:3.12-slim`, `uv:0.12.19` and the Lambda Web Adapter `1.1.0`, fetched 2026-09-28.

## [0.1.0] - 2026-09-27

- 2026-09-07: the course decisions fixed in a grilling session; ADRs 0001 to 0007 in the authoring workspace.
- 2026-09-08: the shared service layer and the four projects built with tests, guide, decks, notebooks and data; `deploy/aws` (CDK) and `deploy/gcp` (Terraform) with a Session path and a Reference stack per track; `deploy/COSTS.md`. Vendor facts and prices are as of this date.
- 2026-09-26: the skill set published to `techwithshadab/claude-skills`.
- 2026-09-27: the participant repository published to `github.com/techwithshadab/northwind-ai-engineering` with the guide on GitHub Pages and the six solutions branches.
