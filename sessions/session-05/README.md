# Session 5: Agentic AI systems

What you build: Project 4, the multi-agent resolution system in `nw/agent`.

```bash
make session05
make agent-eval              # the 15 adversarial tickets through the hand-built loop, traces in artifacts/traces
make replay RUN=<run_id>     # print one trajectory
make specialists             # three specialist agents and the orchestrator on :8010 to :8013
make agent-eval-strands      # AWS track: the same evaluation through Strands
make agent-eval-adk          # GCP track: the same evaluation through ADK
make mcp                     # the registry as an MCP server on :8020
```

Files you edit in this session:

- `nw/agent/tools.py`: `ToolRegistry.validate`
- `nw/agent/northwind.py`: `check_entitlement`
- `nw/agent/loop.py`: `run_agent`
- `nw/agent/evaluate.py`: `score`
- `nw/agent/orchestrator.py`: `orchestrator_registry`

Files you read but do not edit: `trace.py`, `service.py`, `mcp_server.py`, `ports/`.
