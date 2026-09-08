"""GCP track: the Northwind agent through Agent Development Kit, Claude on Vertex AI.

    uv run python -m nw.agent.ports.adk_port

What ADK gives you: the loop, sessions, callbacks, evaluation tooling, and a
path to Vertex AI Agent Engine. What you keep from Session 5: the registry and
the gate, here as `FunctionTool(require_confirmation=True)` on `escalate`, which
pauses the run for a human instead of executing. The adversarial set is unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from nw.agent.evaluate import aggregate, format_report, load_cases, score
from nw.agent.loop import SYSTEM_RULES
from nw.agent.ports import function_for, is_irreversible
from nw.agent.tools import ToolRegistry, untrusted
from nw.agent.trace import ProposedAction, Step, Termination, Trajectory


def build_agent(registry: ToolRegistry, *, model_id: str, project: str, region: str):
    from anthropic import AsyncAnthropicVertex
    from google.adk.agents import LlmAgent
    from google.adk.models.anthropic_llm import Claude
    from google.adk.tools import FunctionTool

    tools = [
        FunctionTool(
            function_for(t, registry), require_confirmation=is_irreversible(registry, t.name)
        )
        for t in registry.tools.values()
    ]
    model = Claude(
        model=model_id,
        max_tokens=1024,
        client=AsyncAnthropicVertex(project_id=project, region=region),
    )
    return LlmAgent(name="resolver_adk", model=model, instruction=SYSTEM_RULES, tools=tools)


async def run_one(
    task: str, registry: ToolRegistry, *, model_id: str, project: str, region: str
) -> Trajectory:
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    agent = build_agent(registry, model_id=model_id, project=project, region=region)
    runner = InMemoryRunner(agent=agent, app_name="northwind")
    session = await runner.session_service.create_session(app_name="northwind", user_id="eval")
    t = Trajectory(run_id=uuid.uuid4().hex[:10], agent="resolver-adk", task=task)
    calls: list[str] = []
    final_parts: list[str] = []
    try:
        message = types.Content(role="user", parts=[types.Part(text=untrusted(task))])
        async for event in runner.run_async(
            user_id="eval", session_id=session.id, new_message=message
        ):
            for call in event.get_function_calls() or []:
                calls.append(call.name)
                if is_irreversible(registry, call.name):
                    t.proposed_actions.append(
                        ProposedAction(
                            tool=call.name, arguments=dict(call.args or {}), step=len(calls)
                        )
                    )
            if event.is_final_response() and event.content and event.content.parts:
                final_parts.extend(p.text for p in event.content.parts if getattr(p, "text", None))
        t.final = "\n".join(final_parts)
        t.terminated = Termination.ANSWER
    except Exception as exc:  # noqa: BLE001
        t.terminated = Termination.ERROR
        t.final = f"Stopped: {type(exc).__name__}: {exc}"
    t.tools_called = list(calls)
    t.steps = [Step(index=i, tool=c) for i, c in enumerate(calls)]
    return t


async def main_async(args: argparse.Namespace) -> int:
    from nw.agent.northwind import build_registry
    from nw.config import ModelRole, settings

    s = settings()
    if s.track.value != "gcp" or not s.gcp_project:
        raise SystemExit("the ADK port runs on the gcp track with NW_GCP_PROJECT set")
    registry = build_registry("local")
    scores = []
    for case in load_cases(args.cases):
        t = await run_one(
            case.task(),
            registry,
            model_id=s.model_for(ModelRole.WORKHORSE),
            project=s.gcp_project,
            region=s.gcp_region,
        )
        t.save(args.traces)
        scores.append(score(case, t))
    agg = aggregate(scores)
    args.out.write_text(
        json.dumps({"aggregate": agg, "scores": [x.model_dump() for x in scores]}, indent=1)
    )
    print(format_report(scores, agg))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=Path, default=Path("data/adversarial/tickets.jsonl"))
    ap.add_argument("--traces", type=Path, default=Path("artifacts/traces-adk"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/agent_eval_adk.json"))
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
