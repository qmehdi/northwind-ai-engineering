# Agentic AI systems

What you build: Project 4, the multi-agent resolution system in `nw/agent`, registered, evaluated by tier and deployed to your tenant's agent runtime.

```bash
make session05
make catalog && make catalog-check && make roles   # the use case catalog, before any code
make agent-eval              # the 15 adversarial tickets, every check labelled with its tier
make replay RUN=<run_id>     # print one trajectory
make approve                 # pending proposals; make approve RUN=<run_id> TOOL=escalate resumes one
make agent-gate-offline      # the tiered gate with a scripted model: free, what CI runs
make agent-gate              # the 15 cases on your track, the Judge scoring the turn tier
make agent-cards             # data/agents/<name>.json from code; PUSH=1 registers them with your runtime
make registry-check          # every card matches the code
make agentops-check          # catalog, registry and the offline gate: what agent-gate.yml runs
make review                  # sample ten recent traces for a person to label
make specialists             # three specialist agents and the orchestrator on :8010 to :8013
make agent-eval-strands      # AWS track: Strands
make agent-eval-adk          # Google Cloud track: Agent Development Kit
make mcp                     # the tool registry as an MCP server on :8020
```

On the platform: `platform.agents.deploy`, `invoke`, `register`, `status` (AgentCore Runtime, Agent Engine, the `resolver` container).

Files you edit: `nw/agent/tools.py` (`ToolRegistry.validate`), `nw/agent/northwind.py` (`check_entitlement`), `nw/agent/loop.py` (`run_agent`), `nw/agent/evaluate.py` (`score`), `nw/agent/orchestrator.py` (`orchestrator_registry`).

Files you read: `data/use_cases.yaml`, `catalog.py`, `registry.py`, `trace.py`, `version.py`, `service.py`, `agentcore.py`, `screen.py`, `monitor.py`, `approve.py`, `review.py`, `offline.py`, `mcp_server.py`, `ports/`.

Operator controls: `NW_AGENT_DISABLED=1`, `NW_AGENT_MAX_CONCURRENT_RUNS` (default 4), `NW_AGENT_MAX_TOTAL_TOKENS`, `NW_AGENT_CAPTURE`, `NW_AGENT_BASELINE`, `NW_AGENT_DRIFT_WINDOW`, `NW_AGENT_DRIFT_MIN`, `NW_ESCALATION_DEDUPE_S`. Endpoints: `/version`, `/drift`, `/metrics`.
