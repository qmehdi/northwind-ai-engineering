# Session 5: Agentic AI systems

What you build: Project 4, the multi-agent resolution system in `nw/agent`.

```bash
make session05
make agent-eval              # the 15 adversarial tickets through the hand-built loop, traces in artifacts/traces
make replay RUN=<run_id>     # print one trajectory
make approve                 # pending proposals; make approve RUN=<run_id> TOOL=escalate resumes one with approval
make agent-gate-offline      # the loop, tools, scorer and gate with a scripted model: free, what CI runs
make agent-gate              # the 15 cases on your track against data/golden/agent_baseline.json (about 2.5 USD)
make review                  # sample ten recent traces into artifacts/review.jsonl for a person to label
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

Files you read but do not edit: `trace.py`, `version.py`, `service.py`, `monitor.py`, `approve.py`, `review.py`, `offline.py`, `mcp_server.py`, `ports/`.

Operator controls on the service, all environment variables: `NW_AGENT_DISABLED=1` (refuse runs, keep readiness), `NW_AGENT_MAX_CONCURRENT_RUNS` (default 4, 429 beyond it), `NW_AGENT_CAPTURE` (one JSON line per run), `NW_AGENT_BASELINE`, `NW_AGENT_DRIFT_WINDOW`, `NW_AGENT_DRIFT_MIN`. Endpoints: `/version`, `/drift`, `/metrics`.
