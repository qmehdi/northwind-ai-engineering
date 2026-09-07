# CLAUDE.md

Read `CONTEXT.md` first for the vocabulary. This file is how to work in the repo.

## Commands

- `make setup`: virtualenv and hooks
- `make check`: what CI runs (ruff, then pytest without `live` tests)
- `make sessionNN`: one session's acceptance tests
- `make preflight`: machine and account check, one model round trip per role on aws or gcp

## Rules

- No em dashes and no en dashes anywhere, code comments included.
- Model access goes through `nw.llm.LLMClient`. Never instantiate a vendor SDK client outside `nw/llm/providers/`.
- Ask for models by role (`ModelRole`), never by ID string.
- Tests must pass with `NW_TRACK=local` and no network. Use `FakeProvider` from `nw/llm/providers/fake.py`.
- Every service sets a correlation ID per request with `bind_correlation_id` and logs with `log_fields`.
- Sampling parameters are dropped for models that reject them (`rejects_sampling`). Do not add them back unconditionally.
- Retries are the client's job, not the SDK's: providers are constructed with `max_retries=0`.

## Working with the agent

The room standardises on the `techwithshadab/skills` skill set, installed in Session 1. Use `/tdd` for the red-to-green exercises, `/code-review` against the session's acceptance criteria, `/diagnosing-bugs` when something spins, and `/session-check` before you call a session done.
