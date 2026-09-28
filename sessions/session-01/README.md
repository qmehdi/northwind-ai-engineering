# Session 1: Foundations and the AI service layer

What you build: `nw/llm`, the client every later project calls a model through.

Follow the guide for the steps. Run the acceptance tests with:

```bash
make session01
```

Files you edit in this session:

- `nw/llm/retry.py`: `RetryPolicy.delay_for`
- `nw/llm/client.py`: `LLMClient.complete`, `_with_retries`, `structured`, `map`

Files you read but do not edit:

- `nw/llm/types.py`, `nw/llm/errors.py`, `nw/llm/provider.py`: the contract
- `nw/llm/providers/`: Bedrock, Vertex, and the fake used by the tests
- `nw/llm/cost.py`, `nw/llm/prices.py`: metering and the price table
- `nw/logging.py`: structured logs and the correlation ID
- `nw/llm/breaker.py`: the circuit breaker per model

Environment, all optional: `NW_MODEL_FALLBACK_WORKHORSE`, `NW_MODEL_FALLBACK_JUDGE`, `NW_MODEL_FALLBACK_ECONOMY` (a second model per role, tried once when the primary fails), `NW_BREAKER_FAILURES` (default 3) and `NW_BREAKER_OPEN_S` (default 30), `NW_REQUEST_TIMEOUT_S` (default 60, applied in the vendor client and around every call). `uv run python -m nw.config` prints every setting with its source and the `config_hash`.
