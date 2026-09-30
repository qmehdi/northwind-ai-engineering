"""GCP track: the Northwind agent through Agent Development Kit, Claude on Vertex AI.

    uv run python -m nw.agent.ports.adk_port

What ADK gives you: the loop, sessions, callbacks, evaluation tooling, and a
path to Vertex AI Agent Engine. What you keep from Project 4: the registry and
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
from typing import Any

from nw.agent.evaluate import aggregate, format_report, load_cases, score
from nw.agent.loop import SYSTEM_RULES
from nw.agent.ports import function_for, is_irreversible
from nw.agent.screen import Screener, from_env
from nw.agent.tools import ToolRegistry, untrusted
from nw.agent.trace import ProposedAction, Step, Termination, Trajectory
from nw.policy.redact import redact_for_agent


def build_agent(
    registry: ToolRegistry,
    *,
    model_id: str,
    project: str,
    region: str,
    screener: Screener | None = None,
):
    from anthropic import AsyncAnthropicVertex
    from google.adk.agents import LlmAgent
    from google.adk.models.anthropic_llm import Claude
    from google.adk.tools import FunctionTool

    tools = [
        FunctionTool(
            function_for(t, registry, screener=screener),
            require_confirmation=is_irreversible(registry, t.name),
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
    task: str,
    registry: ToolRegistry,
    *,
    model_id: str,
    project: str,
    region: str,
    max_steps: int = 10,
    screener: Screener | None = None,
) -> Trajectory:
    from google.adk.agents.run_config import RunConfig
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    agent = build_agent(
        registry, model_id=model_id, project=project, region=region, screener=screener
    )
    task = redact_for_agent(task)  # the same redaction as the hand-built loop, before the model
    runner = InMemoryRunner(agent=agent, app_name="northwind")
    session = await runner.session_service.create_session(app_name="northwind", user_id="eval")
    t = Trajectory(run_id=uuid.uuid4().hex[:10], agent="resolver-adk", task=task)
    calls: list[str] = []
    final_parts: list[str] = []
    paused_on: str | None = None
    try:
        message = types.Content(role="user", parts=[types.Part(text=untrusted(task))])
        # `max_llm_calls` is ADK's step cap: the runner raises once the run exceeds it.
        async for event in runner.run_async(
            user_id="eval",
            session_id=session.id,
            new_message=message,
            run_config=RunConfig(max_llm_calls=max_steps),
        ):
            for call in event.get_function_calls() or []:
                if call.name == "adk_request_confirmation":
                    # `require_confirmation` pauses the run here for a human; the proposal
                    # was recorded when the tool itself was called.
                    paused_on = paused_on or (calls[-1] if calls else call.name)
                    continue
                calls.append(call.name)
                if is_irreversible(registry, call.name):
                    t.proposed_actions.append(
                        ProposedAction(
                            tool=call.name, arguments=dict(call.args or {}), step=len(calls)
                        )
                    )
            if event.is_final_response() and event.content and event.content.parts:
                final_parts.extend(p.text for p in event.content.parts if getattr(p, "text", None))
        finish(t, final_parts, paused_on=paused_on)
    except Exception as exc:  # noqa: BLE001
        capped = "LlmCallsLimit" in type(exc).__name__
        t.terminated = Termination.MAX_STEPS if capped else Termination.ERROR
        t.final = f"Stopped: {type(exc).__name__}: {exc}"
    t.tools_called = list(calls)
    t.steps = [Step(index=i, tool=c) for i, c in enumerate(calls)]
    return t


def finish(t: Trajectory, final_parts: list[str], *, paused_on: str | None = None) -> None:
    """How the run ended, from what the runner yielded. ANSWER only when the model produced
    a final text; a run paused on the confirmation gate is an answer too (the proposal is
    recorded and nothing executed, as in the hand-built loop); a runner that stops with
    neither, its step limit for instance, is an ERROR, never a silent success."""
    text = "\n".join(p for p in final_parts if p).strip()
    if text:
        t.final, t.terminated = text, Termination.ANSWER
    elif paused_on:
        t.final = f"Paused for human confirmation of {paused_on}; the action was proposed, not run."
        t.terminated = Termination.ANSWER
    else:
        t.final = "Stopped: the runner ended without a final response."
        t.terminated = Termination.ERROR


async def main_async(args: argparse.Namespace) -> int:
    from nw.agent.northwind import bind_account, build_registry
    from nw.config import settings

    s = settings()
    if s.track.value != "gcp" or not s.gcp_project:
        raise SystemExit("the ADK port runs on the gcp track with NW_GCP_PROJECT set")
    registry = build_registry("local")
    screener = from_env()
    scores = []
    for case in load_cases(args.cases):
        with bind_account(case.account_id):
            t = await _one(case, registry, s, screener)
        t.save(args.traces)
        scores.append(score(case, t))
    agg = aggregate(scores)
    args.out.write_text(
        json.dumps({"aggregate": agg, "scores": [x.model_dump() for x in scores]}, indent=1)
    )
    print(format_report(scores, agg))
    return 0


async def _one(case: Any, registry: ToolRegistry, s: Any, screener: Screener) -> Trajectory:
    from nw.config import ModelRole

    return await run_one(
        case.task(),
        registry,
        model_id=s.model_for(ModelRole.WORKHORSE),
        project=s.gcp_project,
        region=s.gcp_region,
        screener=screener,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=Path, default=Path("data/adversarial/tickets.jsonl"))
    ap.add_argument("--traces", type=Path, default=Path("artifacts/traces-adk"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/agent_eval_adk.json"))
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
