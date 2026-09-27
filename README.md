# AI Engineering: GenAI and Agentic Systems

The course repository for **AI Engineering: GenAI and Agentic Systems**. Six sessions of two hours. Four projects, each shipped as a running service, on one domain: support-ticket intelligence for Northwind Cloud, a B2B SaaS company.

Northwind Cloud has just hired you as its AI engineer to make the support desk faster without making it dangerous.

## Start here

1. Pick your track, AWS or GCP. You keep it for all six sessions.
2. Install `uv` and Docker.
3. Clone this repository, then:

```bash
make setup
cp .env.example .env      # set NW_TRACK and your account details
make preflight            # paste the table into the cohort channel
```

4. Open the guide: https://techwithshadab.github.io/northwind-ai-engineering

## Layout

| Path | What it is |
| --- | --- |
| `nw/` | The package. Every project lives here and imports from here |
| `nw/llm/` | Session 1: the shared service layer in front of any model provider |
| `sessions/session-NN/` | One README per session pointing at the guide and the files you edit |
| `tests/sessionNN/` | Acceptance tests per session. `make sessionNN` runs one session's tests |
| `notebooks/` | One exploration notebook per session from Session 2. Never the deliverable |
| `data/` | Synthetic Northwind tickets, policy corpus, golden set |
| `deploy/aws`, `deploy/gcp` | Session 6: the Session path and the Reference stack for each track |
| `scripts/` | Preflight and data tooling |

## Conventions

- Code asks for a model by role (`workhorse`, `judge`, `economy`), never by ID. Model IDs live in `nw/config.py` and `.env`.
- Nothing outside `nw/llm/providers/` imports a cloud SDK.
- Every service emits structured JSON logs with a correlation ID, and meters its model spend.
- Tests never need a cloud account. Anything that spends money is marked `live`.
- The exercise tests are red until you do the exercise. `make test` and CI run only the tests the skeleton is expected to pass (`tests/skeleton-green.txt`); `make sessionNN` is how you check your own progress.
- Agent-written code is held to the same bar as anything else: it passes the tests or it does not go in.

## Solutions

After each session, that session's solution is published on the branch `solutions/session-NN`.

## Data and licence

Every ticket, account and policy document here is synthetic, generated for the course; nothing is real customer data. The ticket schema and label mix are modelled on the public Hugging Face dataset Tobi-Bueck/customer-support-tickets (CC BY-NC 4.0), of which no rows are shipped. See `data/README.md`.

The code is MIT licensed (`LICENSE`). The course material is for the cohort's use.

## Getting help

- The guide's Reference tab for each session explains the why; the step's "What to expect" block tells you what right looks like.
- `/session-check N` in your coding agent reads your checkout against the session's acceptance criteria.
- Open an issue on this repository for anything broken in the skeleton; bring anything else to the cohort channel.
