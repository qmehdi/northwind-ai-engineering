# Reference policy documents

Copied from the AWS developer guides on 2026-09-08 (see the instructor research notes,
"AgentCore Runtime IAM"). `stacks/reference.py` builds the same statements with CDK and
adds three the course needs on top (the API key secret, `bedrock:ApplyGuardrail` on the
course guardrail, and the S3 Vectors actions on the policy index); `tests/test_synth.py`
pins the shape.

| File | What |
| --- | --- |
| `agentcore-runtime-trust.json` | Who may assume the runtime execution role: `bedrock-agentcore.amazonaws.com`, pinned to the account and region |
| `agentcore-runtime-execution.json` | The runtime execution role's permissions: ECR pull, its own log group, X-Ray, the `bedrock-agentcore` metric namespace, workload access tokens, and `bedrock:InvokeModel` on the three course models only |

`${AccountId}` and `${Region}` are placeholders; the stack fills them in. The gateway
execution role has no document here: its two statements (`GetPolicyEngine` on the engine,
`AuthorizeAction` and `PartiallyAuthorizeActions` on the engine and the gateway) plus
`InvokeAgentRuntime` on the tools runtime are in `stacks/reference.py` with the devguide
page named in the comment.
