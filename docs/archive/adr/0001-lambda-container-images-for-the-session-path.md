# 0001. Lambda container images for the Session path on AWS

Date: 2026-09-08. Status: superseded on 2026-09-29 by the workspace ADR 0008 (the managed platform replaces the Session path; the policy service still runs as a Lambda container image behind an HTTP API, the rest moved to SageMaker endpoints and AgentCore).

## Context

The Session path is the deployment every participant makes on the AWS track and destroys the same day: four container images, scale to zero, one API key, no idle cost. It must be comparable with the GCP track, where the same images run on Cloud Run. App Runner, the first choice, closed to new AWS customers in 2026. AWS points new customers at ECS Express Mode: Fargate tasks behind a load balancer, always warm, which bills while idle (about 1.20 USD per day for four small tasks plus the load balancer) and adds a VPC, a load balancer and a task definition to a two-hour session.

The services load a model or an index at startup, which takes longer than Lambda's ten-second synchronous init window, and the policy and agent services can run for tens of seconds per request.

## Decision

Deploy the Session path as Lambda container images (`lambda.DockerImageFunction`, arm64) behind function URLs, with the AWS Lambda Web Adapter in the image so the same uvicorn process serves everywhere. Readiness waits on `/readyz`, async init lets model loading run past the init window, function timeouts and memory are set per service. The course API key, not the function URL's IAM auth, protects the URL, so the GCP track and the local stack take the same header. ECS Express Mode stays documented as the always-warm alternative in `deploy/aws/README.md` and `deploy/COSTS.md`, not in the repository.

## Consequences

- Idle costs nothing; a cold start costs two to ten seconds depending on the image, which the latency step measures on purpose.
- One Dockerfile serves Lambda, Cloud Run and docker compose; the `LAMBDA` build argument adds the adapter and nothing else changes.
- Function URL auth is `NONE` at the platform level; the API key middleware in `nw/auth.py` and the rate limiter in `nw/ratelimit.py` are the perimeter (see 0002).
- Lambda's payload and duration limits bound the agent's runs, which the concurrency cap and the step cap already do.
- If AWS reopens App Runner or ECS Express Mode gains scale to zero, revisit; the CDK stack isolates the choice in `deploy/aws/stacks/session_path.py`.
