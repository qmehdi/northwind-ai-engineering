"""The trajectory: every step of an agent run, replayable from a file.

A trace is the unit of debugging and of evaluation. The Project 4 eval
scores trajectories, not just final answers, and `replay` prints one so a
failed run can be read step by step without re-running it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from nw.agent.opstore import OpsStore

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def valid_run_id(run_id: str) -> str:
    """A run id is a file and an object name: letters, digits, dash and underscore only, so an
    id built from a request (a ticket id on the router's P0 branch) cannot write outside the
    trace directory or overwrite another run's file."""
    if not RUN_ID_RE.match(run_id or ""):
        raise ValueError(f"invalid run id {run_id!r}")
    return run_id


class Termination(StrEnum):
    ANSWER = "answer"
    MAX_STEPS = "max_steps"
    BUDGET = "budget"
    ERROR = "error"


class Step(BaseModel):
    index: int
    thought: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    observation: str | None = None
    ok: bool = True
    pending_approval: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0


class ProposedAction(BaseModel):
    tool: str
    arguments: dict[str, Any]
    step: int


class Trajectory(BaseModel):
    run_id: str
    agent: str
    task: str
    started_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    steps: list[Step] = Field(default_factory=list)
    final: str | None = None
    terminated: Termination = Termination.ANSWER
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    cost_usd: float = 0.0
    tools_called: list[str] = Field(default_factory=list)
    correlation_id: str | None = None
    agent_version: str | None = None  # hash of prompt, tool specs and model ids, see version.py
    model_id: str | None = None  # the model the role resolved to when the run started
    resumed_from: str | None = None  # the run whose proposal this run executed with approval
    tokens_total: int = 0  # input plus output over every model call in the run
    account_id: str | None = None  # the account the run's tools were bound to, from the request
    requested_by: str | None = None  # who started the run: the caller's key id or identity
    approved_by: str | None = None  # on an approval run: who approved the recorded action

    @property
    def n_steps(self) -> int:
        return len(self.steps)

    def redacted(self) -> Trajectory:
        """A copy safe to keep: every free-text field (task, thoughts, arguments, observations,
        the final reply, proposed arguments) through the agent path's redaction. Account and
        invoice ids stay, they are what the run was bound to and what an approval executes."""
        from nw.policy.redact import redact_for_agent, redact_value

        keep = ("ACCOUNT", "INVOICE")
        out = self.model_copy(deep=True)
        out.task = redact_for_agent(out.task)
        out.final = redact_for_agent(out.final) if out.final else out.final
        for s in out.steps:
            s.thought = redact_for_agent(s.thought) if s.thought else s.thought
            s.observation = redact_for_agent(s.observation) if s.observation else s.observation
            s.arguments = redact_value(s.arguments, keep=keep) if s.arguments else s.arguments
        for p in out.proposed_actions:
            p.arguments = redact_value(p.arguments, keep=keep)
        return out

    def save(self, directory: Path | OpsStore) -> Path | str:
        """Write the redacted trajectory as `<run_id>.json` to a directory or an ops store
        (`nw.agent.opstore`); returns where it went. Nothing unredacted is ever written."""
        name = f"{valid_run_id(self.run_id)}.json"
        data = self.redacted().model_dump_json(indent=1)
        if isinstance(directory, Path | str):
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / name
            path.write_text(data)
            return path
        return directory.put(name, data.encode())

    @classmethod
    def load(cls, path: Path) -> Trajectory:
        return cls.model_validate_json(path.read_text())

    @classmethod
    def load_from(cls, store: OpsStore, run_id: str) -> Trajectory:
        return cls.model_validate_json(store.get(f"{valid_run_id(run_id)}.json"))


def replay(t: Trajectory, width: int = 110) -> str:
    """A readable transcript of a run."""
    lines = [
        f"run {t.run_id}  agent={t.agent}  terminated={t.terminated.value}  "
        f"steps={t.n_steps}  cost={t.cost_usd:.4f} USD"
    ]
    if t.agent_version or t.model_id or t.resumed_from:
        lines.append(
            f"agent_version={t.agent_version or '?'}  model_id={t.model_id or '?'}"
            + (f"  tokens={t.tokens_total}" if t.tokens_total else "")
            + (f"  resumed_from={t.resumed_from}" if t.resumed_from else "")
        )
    lines.append(f"task: {t.task[:width]}")
    for s in t.steps:
        head = f"[{s.index:02d}] "
        if s.thought:
            lines.append(head + "thought: " + s.thought[:width])
            head = "     "
        if s.tool:
            lines.append(head + f"call {s.tool}({json.dumps(s.arguments, default=str)[:width]})")
            mark = "PENDING APPROVAL" if s.pending_approval else ("ok" if s.ok else "ERROR")
            lines.append(f"     -> {mark}: {(s.observation or '')[:width]}")
        lines.append(
            f"     tokens {s.input_tokens}+{s.output_tokens}  {s.latency_ms:.0f} ms  "
            f"{s.cost_usd:.5f} USD"
        )
    if t.proposed_actions:
        lines.append("proposed actions awaiting approval:")
        lines += [
            f"  {p.tool}({json.dumps(p.arguments, default=str)[:width]})"
            for p in t.proposed_actions
        ]
    lines.append(f"final: {(t.final or '')[: width * 3]}")
    return "\n".join(lines)


def main() -> int:
    """`python -m nw.agent.trace artifacts/traces/<run_id>.json` prints a trajectory."""
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m nw.agent.trace <trace.json>")
        return 2
    print(replay(Trajectory.load(Path(sys.argv[1]))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
