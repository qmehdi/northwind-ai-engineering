# Agentic AI systems

What you build: Project 4, the multi-agent resolution system in `nw/agent`: registered before it is built, bound to the ticket's account, evaluated by tier on 35 cases with pass^3, gated against a provenance-checked baseline, approved by a person, and deployed to your tenant's agent runtime.

```bash
make session05                # the acceptance tests (167 on the solution)
make catalog && make catalog-check && make roles   # the use case catalog, before any code
make governance-check         # catalog, agent cards and the data manifest
make index                    # the similar-ticket index find_similar_tickets reads (needed before any run)
uv run python -m nw.agent.evaluate --no-judge   # the 35 cases (17 adversarial, 18 benign), every failure labelled with its tier
make agent-eval               # the same with the turn Judge (NO_JUDGE=1 here and on agent-gate, agent-gate-live runs without it; with no Judge route a judged run is refused, exit 2)
make replay RUN=<run_id>      # print one trajectory
make approve                  # pending proposals; make approve RUN=<run_id> TOOL=escalate executes the recorded action once, as you
make agent-gate-offline       # the tiered gate with a scripted model: free, what CI runs
make agent-calibrate-judge    # the turn Judge against 40 graded turns; the live gate needs it
make agent-gate-live          # every case K times (K=3) on your track; the gate reads pass^3
make agent-record             # a live run that records every completion (CASSETTE=...)
make agent-replay             # the recorded run through today's loop and tools, no model
make agent-baseline-offline   # rewrite the offline baseline after a case file changed
make agent-cards              # data/agents/<name>.json from code; PUSH=1 registers them with your runtime
make registry-check           # every card matches the code
make agentops-check           # catalog, registry and the offline gate: what agent-gate.yml runs
make review                   # sample ten recent traces for a person to label
make specialists              # three specialist agents and the orchestrator on :8010 to :8013
make agent-eval-strands       # AWS track: Strands
make agent-eval-adk           # Google Cloud track: Agent Development Kit
make mcp                      # the tool registry as an MCP server on :8020
```

Baselines live in `data/golden/baselines/`: `agent-offline.json` for the scripted run, `agent-<track>.json` for a live track, each with its provenance (mode, track, model ids, Judge, case-file hash, repeats). `data/golden/agent_baseline.json` is the legacy Claude-era run, refused by the gate.

On the platform: `platform.agents.deploy`, `invoke`, `register`, `status` (AgentCore Runtime on AWS, the Agent Runtime on Google Cloud, a Foundry hosted agent on Azure, the `resolver` container on Local). Trajectories, approvals and the escalation queue live in the ops store (`NW_OPS_STORE`) under `<environment>-<tenant>/` on the cloud tracks; to approve there, set `NW_OPS_STORE`, `NW_ENVIRONMENT` and `NW_TENANT` and run `make approve` as yourself.

Files you edit: `nw/agent/tools.py` (`ToolRegistry.validate`), `nw/agent/northwind.py` (`check_entitlement`), `nw/agent/loop.py` (`_loop`), `nw/agent/evaluate.py` (`score`), `nw/agent/orchestrator.py` (`orchestrator_registry`).

Files you read: `data/use_cases.yaml`, `catalog.py`, `registry.py`, `trace.py`, `version.py`, `service.py`, `agentcore.py`, `screen.py`, `monitor.py`, `approve.py`, `opstore.py`, `review.py`, `offline.py`, `mcp_server.py`, `mcp_client.py`, `toolauth.py`, `ports/`, `nw/evalstats.py`, `docs/adr/0005-approval-gate-enforced-twice.md`, `docs/governance/`.

Operator controls: `NW_AGENT_DISABLED=1`, `NW_AGENT_MAX_CONCURRENT_RUNS` (default 4), `NW_AGENT_MAX_TOTAL_TOKENS`, `NW_AGENT_CAPTURE`, `NW_AGENT_BASELINE`, `NW_AGENT_DRIFT_WINDOW`, `NW_AGENT_DRIFT_MIN` (default 200), `NW_AGENT_JUDGE_SAMPLE` (default 0.05), `NW_ESCALATION_DEDUPE_S`, `NW_OPS_STORE`, `NW_REDACT_DETECTOR`, `NW_TOOL_BACKEND` (`local`, `http` or `mcp`, with `NW_MCP_URL` and `NW_MCP_AUTH`). Endpoints: `/run` (with `account_id`), `/route`, `/version`, `/drift`, `/metrics`.
