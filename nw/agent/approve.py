"""Approve and resume: the human half of the approval gate.

The loop never executes an irreversible tool on its own; it records a proposed action
and moves on. Someone has to look at the proposal and say yes. This is that someone's
tool:

    uv run python -m nw.agent.approve                       # every pending proposal in the traces
    uv run python -m nw.agent.approve --run <id> --approve escalate

Approving re-runs the parent trajectory's task with an approval policy that says yes to
exactly that tool with exactly those arguments and no to everything else. The new run is
saved as its own trajectory with `resumed_from` pointing at the parent, so the queue line
the tool writes can be traced to the proposal and to the person who approved it. A model
that comes back with different arguments (a reworded justification, another tier) gets a
new proposal, not an execution: the approval was for what the person read, not for the
tool in general.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from nw.agent.loop import SYSTEM_RULES, ApprovalPolicy, run_agent
from nw.agent.tools import ToolRegistry
from nw.agent.trace import Trajectory, replay
from nw.llm import LLMClient
from nw.logging import get_logger, log_fields

log = get_logger("nw.agent.approve")


def load_trajectories(traces: Path) -> list[Trajectory]:
    """Every readable trace, oldest first. Unreadable files are skipped, not fatal."""
    out: list[Trajectory] = []
    for path in sorted(traces.glob("*.json")):
        try:
            out.append(Trajectory.load(path))
        except ValueError:
            continue
    return sorted(out, key=lambda t: t.started_at)


def list_proposals(traces: Path) -> list[dict[str, Any]]:
    """One row per proposed action that no later run has resumed."""
    ts = load_trajectories(traces)
    resumed = {t.resumed_from for t in ts if t.resumed_from}
    rows: list[dict[str, Any]] = []
    for t in ts:
        if t.run_id in resumed:
            continue
        for p in t.proposed_actions:
            rows.append(
                {
                    "run_id": t.run_id,
                    "agent": t.agent,
                    "started_at": t.started_at,
                    "tool": p.tool,
                    "arguments": p.arguments,
                    "step": p.step,
                    "task": t.task.splitlines()[0][:80],
                }
            )
    return rows


def format_proposals(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no pending proposals"
    lines = [
        "| Run | Step | Tool | Arguments | Task |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for r in rows:
        args = json.dumps(r["arguments"], default=str)
        lines.append(
            f"| {r['run_id']} | {r['step']} | {r['tool']} | {args[:90]} | {r['task'][:50]} |"
        )
    lines.append(f"{len(rows)} pending. Approve one: --run <id> --approve <tool>")
    return "\n".join(lines)


def approve_exactly(tool: str, arguments: dict[str, Any]) -> ApprovalPolicy:
    """Yes to this tool with these arguments; no to anything else, including the same tool
    with different arguments."""

    def policy(name: str, args: dict[str, Any]) -> bool:
        return name == tool and args == arguments

    return policy


async def resume(
    run_id: str,
    tool: str,
    registry: ToolRegistry,
    client: LLMClient,
    *,
    traces: Path,
    arguments: dict[str, Any] | None = None,
    approved_by: str = "operator",
    **run_kw: Any,
) -> Trajectory:
    parent = Trajectory.load(traces / f"{run_id}.json")
    matches = [
        p
        for p in parent.proposed_actions
        if p.tool == tool and (arguments is None or p.arguments == arguments)
    ]
    if not matches:
        raise LookupError(f"run {run_id} has no pending proposal for {tool}")
    if len(matches) > 1 and arguments is None:
        raise LookupError(
            f"run {run_id} proposed {tool} {len(matches)} times; pass --arguments to pick one"
        )
    chosen = matches[0]
    system, reg = _context(parent.agent, registry)
    log.info(
        "approved",
        extra=log_fields(
            run_id=run_id, tool=tool, arguments=chosen.arguments, approved_by=approved_by
        ),
    )
    t = await run_agent(
        parent.task,
        reg,
        client,
        system=system,
        approval=approve_exactly(tool, chosen.arguments),
        agent_name=parent.agent,
        **run_kw,
    )
    t.resumed_from = parent.run_id
    t.save(traces)
    return t


def _context(agent: str, registry: ToolRegistry) -> tuple[str, ToolRegistry]:
    """A specialist resumes with its own prompt and tool subset; anything else with the
    resolver's."""
    from nw.agent.orchestrator import SPECIALISTS, subset

    if agent in SPECIALISTS:
        return SPECIALISTS[agent]["system"], subset(registry, SPECIALISTS[agent]["tools"])
    return SYSTEM_RULES, registry


async def main_async(args: argparse.Namespace) -> int:
    if not args.run:
        print(format_proposals(list_proposals(args.traces)))
        return 0
    if not args.approve:
        print("usage: --run <id> --approve <tool>")
        return 2
    from nw.agent.northwind import build_registry
    from nw.config import settings
    from nw.llm.providers import make_provider

    s = settings()
    client = LLMClient(make_provider(s), settings=s)
    arguments = json.loads(args.arguments) if args.arguments else None
    try:
        t = await resume(
            args.run,
            args.approve,
            build_registry(args.backend),
            client,
            traces=args.traces,
            arguments=arguments,
            approved_by=args.by,
        )
    except LookupError as exc:
        print(exc)
        return 1
    print(replay(t))
    if t.proposed_actions:
        print(
            "\nthe model proposed again with different arguments; nothing was executed. "
            "Read the new proposal and approve that one if it is right."
        )
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", type=Path, default=Path("artifacts/traces"))
    ap.add_argument("--run", help="the run id whose proposal you approve")
    ap.add_argument("--approve", help="the tool to approve, exactly as proposed")
    ap.add_argument("--arguments", help="JSON, to pick one of several proposals of the tool")
    ap.add_argument("--backend", default="local")
    ap.add_argument("--by", default="operator", help="who approved, for the log line")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
