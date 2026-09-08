"""AWS track: the Northwind agent through Strands Agents, on Bedrock.

    uv run python -m nw.agent.ports.strands_port

What Strands gives you: the loop, retries, streaming, session state, and
OpenTelemetry traces out of the box. What you keep from Session 5: the
registry (validation, errors as observations) and the approval gate, enforced
here with a `BeforeToolCallEvent` hook that cancels `escalate` and records it
as a proposal. The adversarial set and its scoring are unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from nw.agent.evaluate import aggregate, format_report, load_cases, score
from nw.agent.loop import SYSTEM_RULES
from nw.agent.ports import function_for, is_irreversible
from nw.agent.tools import ToolRegistry, untrusted
from nw.agent.trace import ProposedAction, Step, Termination, Trajectory


def build_agent(
    registry: ToolRegistry,
    *,
    model_id: str,
    region: str,
    proposals: list[ProposedAction],
    calls: list[str],
):
    from strands import Agent, tool
    from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
    from strands.models.bedrock import BedrockModel

    tools = [tool(function_for(t, registry, sync=True)) for t in registry.tools.values()]

    class Gate(HookProvider):
        def register_hooks(self, hooks: HookRegistry, **kwargs: Any) -> None:
            hooks.add_callback(BeforeToolCallEvent, self.before)

        def before(self, event: BeforeToolCallEvent) -> None:
            name = event.tool_use["name"]
            calls.append(name)
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


def run_one(
    task: str, registry: ToolRegistry, *, model_id: str, region: str, max_steps: int = 10
) -> Trajectory:
    proposals: list[ProposedAction] = []
    calls: list[str] = []
    agent = build_agent(
        registry, model_id=model_id, region=region, proposals=proposals, calls=calls
    )
    t = Trajectory(run_id=uuid.uuid4().hex[:10], agent="resolver-strands", task=task)
    try:
        result = agent(untrusted(task))
        text = "".join(
            b.get("text", "") for b in result.message.get("content", []) if isinstance(b, dict)
        )
        t.final = text
        t.terminated = (
            Termination.ANSWER if result.stop_reason == "end_turn" else Termination.MAX_STEPS
        )
        usage = getattr(result.metrics, "accumulated_usage", {}) or {}
        t.steps = [Step(index=i, tool=c) for i, c in enumerate(calls)]
        if t.steps:
            t.steps[0].input_tokens = int(usage.get("inputTokens", 0))
            t.steps[0].output_tokens = int(usage.get("outputTokens", 0))
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
    model_id = s.model_for(ModelRole.WORKHORSE)
    # Strands talks to Bedrock through boto3 (Converse), which takes the bare Bedrock model id.
    registry = build_registry("local")
    scores = []
    for case in load_cases(args.cases):
        t = run_one(case.task(), registry, model_id=model_id, region=s.aws_region)
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
