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
