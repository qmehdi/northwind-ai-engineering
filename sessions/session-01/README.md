# Foundations and the AI service layer

What you build: `nw/llm`, the client every later project calls a model through.

Follow the guide for the steps, on your track (AWS, Google Cloud, Azure or Local). Run the acceptance tests with:

```bash
make session01                      # 219 tests; the skeleton passes 175 and fails 44
NW_PROVIDER=fake make preflight     # every role on the fake provider, the mode the exercises run in
make preflight                      # one route line and one round trip per role, through the gateway when NW_GATEWAY_URL is set
uv run python scripts/live_check.py # the first live call, on the Workhorse (gpt-oss-120b on the clouds; gpt-oss:20b, or qwen3:4b with SMALL=1, on Local)
```

Files you edit in this part:

- `nw/llm/retry.py`: `RetryPolicy.delay_for`
- `nw/llm/client.py`: `LLMClient._with_retries`, `complete`, `structured`, `map`

Files you read but do not edit:

- `nw/llm/types.py`, `nw/llm/errors.py`, `nw/llm/provider.py`: the contract
- `nw/llm/providers/`: Bedrock Converse (gpt-oss, Nova), the OpenAI-compatible client (Ollama, the model gateway, Google's managed API), Microsoft Foundry directly or behind API Management, Claude on Bedrock, the Agent Platform, Foundry or the Anthropic API, the `RoleRouter`, and the fake used by the tests
- `nw/llm/cost.py`, `nw/llm/prices.py`: metering, cost scopes and the price table
- `nw/llm/residency.py`: EU residency routing
- `nw/llm/breaker.py`: the circuit breaker per model
- `nw/logging.py`: structured logs and the correlation ID
- `nw/auth.py`, `nw/ratelimit.py`: the API key middleware every service runs behind, failing closed off the Local track

Environment: `NW_TRACK`, `NW_TENANT`, `NW_ENVIRONMENT`, `NW_GATEWAY_URL`, `NW_GATEWAY_KEY` name you on the platform (on Azure the gateway URL comes from `deploy/azure/outputs.json` as `NW_AZURE_APIM_GATEWAY_URL`); `NW_PROVIDER=fake` answers every role in memory. On Local the Judge needs `ANTHROPIC_API_KEY` on the gateway or `NW_MODEL_JUDGE=judge` for the local stand-in (`deploy/local/README.md`). All optional: `NW_MODEL_FALLBACK_WORKHORSE`, `NW_MODEL_FALLBACK_JUDGE`, `NW_MODEL_FALLBACK_ECONOMY` (a second model per role, tried once when the primary is unavailable or exhausted), `NW_BREAKER_FAILURES` (default 3) and `NW_BREAKER_OPEN_S` (default 30), `NW_REQUEST_TIMEOUT_S` (default 60, per attempt), `NW_CALL_DEADLINE_S` (default 180, over every attempt, back-off and fallback of one call), `NW_RESIDENCY_ROUTING` (default on) and `NW_MODEL_EU_<ROLE>`. `uv run python -m nw.config` prints every setting with its source and the `config_hash`.

Reference: `docs/SECURITY.md` for keys and fail-closed services, `docs/governance/residency-and-subprocessors.md` for where each model call is processed.
