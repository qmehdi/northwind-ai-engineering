"""Trace review sampling: the human loop that feeds the adversarial set.

Metrics say how often runs end badly; only a person reading a trace says why. Reading
every trace does not scale, so sample, and sample where the failures hide: runs that
did not end in an answer, and runs that proposed an irreversible action.

    uv run python -m nw.agent.review --sample 10 --out artifacts/review.jsonl
    # ... a person fills in `label` and `note` on each line ...
    uv run python -m nw.agent.review --to-cases artifacts/review.jsonl \
        --out data/adversarial/candidates.jsonl

A labelled row becomes a candidate case in the adversarial file's format, with the
expectations a scorer can check derived from the label; the author edits the `expect`
block before committing it. The adversarial set grows from production, not from
imagination.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

from nw.agent.approve import load_trajectories
from nw.agent.trace import Termination, Trajectory

SUBJECT = re.compile(r"^Subject:\s*(.*)$", re.MULTILINE)
HEADER = re.compile(r"Ticket (T-\d{6}) from account (NW-\d{5,})")
RECENT = 500


def weight(t: Trajectory) -> float:
    """Non-answers and proposals are where the review effort pays."""
    w = 1.0
    if t.terminated is not Termination.ANSWER:
        w += 3.0
    if t.proposed_actions:
        w += 2.0
    return w


def sample(trajectories: list[Trajectory], n: int, seed: int | None = None) -> list[Trajectory]:
    """Weighted sampling without replacement over the most recent `RECENT` runs."""
    pool = list(trajectories[-RECENT:])
    rng = random.Random(seed)
    picked: list[Trajectory] = []
    while pool and len(picked) < n:
        weights = [weight(t) for t in pool]
        chosen = rng.choices(range(len(pool)), weights=weights, k=1)[0]
        picked.append(pool.pop(chosen))
    return picked


def to_row(t: Trajectory) -> dict[str, Any]:
    return {
        "run_id": t.run_id,
        "agent": t.agent,
        "agent_version": t.agent_version,
        "task": t.task,
        "tools_called": t.tools_called,
        "proposed": [
            f"{p.tool}({json.dumps(p.arguments, default=str)})" for p in t.proposed_actions
        ],
        "final": t.final,
        "terminated": t.terminated.value,
        "steps": t.n_steps,
        "cost_usd": round(t.cost_usd, 5),
        "label": "",
        "note": "",
    }


def write_rows(rows: list[dict[str, Any]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def to_case(row: dict[str, Any], index: int) -> dict[str, Any] | None:
    """A labelled row as an adversarial case. Labels: `should_escalate`, `should_not_escalate`,
    or any other word, which becomes the case's kind with a must-not-escalate expectation
    unless the run proposed one and the label says it was right."""
    label = (row.get("label") or "").strip().lower()
    if not label or label in {"ok", "pass", "fine"}:
        return None
    task = row.get("task") or ""
    m = HEADER.search(task)
    ticket_id, account_id = (m.group(1), m.group(2)) if m else ("T-000000", "NW-00000")
    subj = SUBJECT.search(task)
    subject = subj.group(1).strip() if subj else task.splitlines()[0][:120]
    body = task.split("\n\n", 1)[1] if "\n\n" in task else task
    expect: dict[str, Any] = {}
    if label == "should_escalate":
        expect["must_escalate"] = True
    else:
        expect["must_not_escalate"] = True
    if row.get("tools_called"):
        expect["expected_tools_any"] = sorted(set(row["tools_called"]))
    kind = label.replace(" ", "_")
    return {
        "id": f"rev-{index:02d}-{row.get('run_id', 'unknown')}",
        "kind": kind,
        "account_id": account_id,
        "ticket_id": ticket_id,
        "subject": subject,
        "body": body,
        "expect": expect,
        "note": row.get("note", ""),
        "source_run": row.get("run_id"),
    }


def to_cases(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases = (to_case(r, i + 1) for i, r in enumerate(rows))
    return [c for c in cases if c is not None]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", type=Path, default=Path("artifacts/traces"))
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", type=Path, default=Path("artifacts/review.jsonl"))
    ap.add_argument(
        "--to-cases", type=Path, metavar="REVIEW", help="turn labelled rows into candidate cases"
    )
    args = ap.parse_args()
    if args.to_cases:
        cases = to_cases(read_rows(args.to_cases))
        write_rows(cases, args.out)
        print(f"{len(cases)} candidate cases written to {args.out}; edit each expect block")
        return 0
    ts = load_trajectories(args.traces)
    if not ts:
        print(f"no traces under {args.traces}")
        return 1
    rows = [to_row(t) for t in sample(ts, args.sample, args.seed)]
    write_rows(rows, args.out)
    ends = sum(1 for r in rows if r["terminated"] != "answer")
    props = sum(1 for r in rows if r["proposed"])
    print(
        f"{len(rows)} of {len(ts)} runs sampled to {args.out}: {ends} not answered, "
        f"{props} with proposals. Fill in label and note."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
