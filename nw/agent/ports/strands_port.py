"""AWS track: the Northwind agent through Strands Agents, on Bedrock.

    uv run python -m nw.agent.ports.strands_port

What Strands gives you: the loop, retries, streaming, session state, and
OpenTelemetry traces out of the box. What you keep from Project 4: the
registry (validation, errors as observations) and the approval gate, enforced
here with a `BeforeToolCallEvent` hook that cancels `escalate` and records it
as a proposal. The adversarial set and its scoring are unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
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
    region: str,
    proposals: list[ProposedAction],
    calls: list[str],
    max_steps: int = 10,
    screener: Screener | None = None,
):
    from strands import Agent, tool
    from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
    from strands.models.bedrock import BedrockModel

    tools = [
        tool(function_for(t, registry, sync=True, screener=screener))
        for t in registry.tools.values()
    ]

    class Gate(HookProvider):
        def register_hooks(self, hooks: HookRegistry, **kwargs: Any) -> None:
            hooks.add_callback(BeforeToolCallEvent, self.before)

        def before(self, event: BeforeToolCallEvent) -> None:
            name = event.tool_use["name"]
            calls.append(name)
            # Strands has no step cap of its own: the hook is the cap. At the cap the call is
            # cancelled with a message the model can read; past it, the run is stopped.
            if len(calls) > max_steps + 2:
                raise StepCapReached(f"stopped: reached the step cap of {max_steps}")
            if len(calls) > max_steps:
                event.cancel_tool = f"step cap of {max_steps} reached; answer now without tools"
                return
            if is_irreversible(registry, name):
                proposals.append(
                    ProposedAction(
                        tool=name, arguments=dict(event.tool_use["input"]), step=len(calls)
                    )
                )
                event.cancel_tool = (
                    f"{name} requires human approval; recorded as a proposal, not executed."
                )

    model = BedrockModel(region_name=region, model_id=model_id, max_tokens=1024)
    return Agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_RULES,
        hooks=[Gate()],
        name="resolver-strands",
    )


class StepCapReached(RuntimeError):
    pass


def run_one(
    task: str,
    registry: ToolRegistry,
    *,
    model_id: str,
    region: str,
    max_steps: int = 10,
    screener: Screener | None = None,
) -> Trajectory:
    proposals: list[ProposedAction] = []
    calls: list[str] = []
    agent = build_agent(
        registry,
        model_id=model_id,
        region=region,
        proposals=proposals,
        calls=calls,
        max_steps=max_steps,
        screener=screener,
    )
    task = redact_for_agent(task)  # the same redaction as the hand-built loop, before the model
    t = Trajectory(run_id=uuid.uuid4().hex[:10], agent="resolver-strands", task=task)
    try:
        result = agent(untrusted(task))
        text = "".join(
            b.get("text", "") for b in result.message.get("content", []) if isinstance(b, dict)
        )
        t.final = text
        t.terminated = (
            Termination.ANSWER
            if result.stop_reason == "end_turn" and len(calls) <= max_steps
            else Termination.MAX_STEPS
        )
        usage = getattr(result.metrics, "accumulated_usage", {}) or {}
        t.steps = [Step(index=i, tool=c) for i, c in enumerate(calls)]
        if t.steps:
            t.steps[0].input_tokens = int(usage.get("inputTokens", 0))
            t.steps[0].output_tokens = int(usage.get("outputTokens", 0))
    except StepCapReached as exc:
        t.terminated = Termination.MAX_STEPS
        t.final = str(exc)
    except Exception as exc:  # noqa: BLE001
        t.terminated = Termination.ERROR
        t.final = f"Stopped: {type(exc).__name__}: {exc}"
    t.tools_called = list(calls)
    t.proposed_actions = proposals
    return t


def main() -> int:
    from nw.agent.northwind import build_registry
    from nw.config import ModelRole, settings

    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=Path, default=Path("data/adversarial/tickets.jsonl"))
    ap.add_argument("--traces", type=Path, default=Path("artifacts/traces-strands"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/agent_eval_strands.json"))
    args = ap.parse_args()
    s = settings()
    if s.track.value != "aws":
        raise SystemExit("the Strands port runs on the aws track")
    # Strands talks to Bedrock through boto3 (Converse). The Converse API may want a versioned
    # id or an inference profile rather than the Mantle id the course client uses, so the id
    # is overridable: NW_MODEL_STRANDS=global.anthropic.claude-sonnet-5... if a 400 says so.
    model_id = os.environ.get("NW_MODEL_STRANDS") or s.model_for(ModelRole.WORKHORSE)
    registry = build_registry("local")
    screener = from_env()
    scores = []
    for case in load_cases(args.cases):
        t = run_one(
            case.task(), registry, model_id=model_id, region=s.aws_region, screener=screener
        )
        t.save(args.traces)
        scores.append(score(case, t))
    agg = aggregate(scores)
    args.out.write_text(
        json.dumps({"aggregate": agg, "scores": [x.model_dump() for x in scores]}, indent=1)
    )
    print(format_report(scores, agg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
