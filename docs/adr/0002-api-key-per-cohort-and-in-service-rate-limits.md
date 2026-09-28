# 0002. One API key per cohort, with key ids and in-service rate limiting, instead of an API gateway

Date: 2026-09-15. Status: accepted.

## Context

Every deployed service spends model tokens. A public URL with no credential is an open wallet; a per-participant identity system is a course in itself. The two tracks would each need their own gateway product (API Gateway with usage plans, Apigee or Cloud Endpoints), each with its own configuration language, cost and a half hour of the session. The Session path is deployed and destroyed within a day, by people who have never seen the account before.

Rate limiting still matters: a loop in one participant's notebook must not empty the shared spend cap, and a leaked key must be attributable.

## Decision

One secret per cohort, injected from the cloud secret store as `NW_API_KEY`, checked by the same middleware in every service (`nw/auth.py`). The secret may be a JSON map of key id to secret, so a cohort, the instructor and the agent's service-to-service calls can carry different ids and one can be rotated without the others. The key id, never the secret, goes on every request line and on `nw_requests_by_key_total`. A token bucket per key id lives in the service (`nw/ratelimit.py`), configured by `NW_RATE_LIMIT_RPS` and `NW_RATE_LIMIT_BURST`, answering 429 with `Retry-After`. Probes and `/metrics` stay open.

No API gateway on the Session path. The Reference stack fronts the agent with the track's agent runtime (AgentCore, Agent Engine), which brings its own identity and policy layer; that is where a per-user identity belongs when a cohort outgrows a shared key.

## Consequences

- One header works on the laptop, in docker compose, on Lambda and on Cloud Run; the guide teaches it once.
- The limiter is per process: two warm instances each allow the full rate. The spend cap in `nw/llm/cost.py` is the hard stop; the limiter is the fairness lever.
- Without a key the limiter falls back to the client address from `x-forwarded-for`, which a client can spoof. Deployed services always have a key, so this only concerns the local stack.
- Rotation is a secret update and a restart; revocation of one id is removing it from the map.
- Attribution is per key id, not per person. A cohort that needs per-person accountability moves to the Reference stack's runtime identity.
