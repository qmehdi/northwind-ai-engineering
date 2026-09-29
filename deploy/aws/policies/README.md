# Reference policy documents

Copied from the AWS developer guides on 2026-09-08 (see the instructor research notes,
"AgentCore Runtime IAM"). `stacks/areas/agents.py` builds the same statements with CDK and adds
what the course needs on top (the API key and gateway key secrets, `bedrock:ApplyGuardrail` on
the course guardrail, `bedrock:Retrieve` on the knowledge bases, Memory and Identity actions,
`sagemaker:InvokeEndpoint` on the platform's endpoints); `tests/test_synth.py` pins the shape.

| File | What |
| --- | --- |
| `agentcore-runtime-trust.json` | Who may assume the runtime execution role: `bedrock-agentcore.amazonaws.com`, pinned to the account and region |
| `agentcore-runtime-execution.json` | The runtime execution role's permissions: ECR pull, its own log group, X-Ray, the `bedrock-agentcore` metric namespace, workload access tokens, and `bedrock:InvokeModel` on the course models only |

`${AccountId}` and `${Region}` are placeholders; the stack fills them in. Three more roles come
from documented pages and are built in code with the page named in the comment: the gateway
execution role (devguide policy-permissions), the evaluations execution role (devguide
evaluations-prerequisites, fetched 2026-09-29) and the knowledge base service role (Bedrock
kb-permissions, fetched 2026-09-29).
