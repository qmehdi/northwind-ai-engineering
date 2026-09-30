"""The feedback loop: what agents say about answers becomes the next golden cases.

`POST /feedback` on the service appends one line per verdict to `NW_POLICY_FEEDBACK`
(default `artifacts/policy/feedback.jsonl`): the verdict and note, plus the answer's
question, audience, citations, confidence, whether it refused, and the prompt version,
index hash and model that produced it. A wrong answer with its citations is most of a
golden row already; this module turns the file into candidates for a person to finish.

    uv run python -m nw.policy.feedback                 # counts by verdict, the notes
    uv run python -m nw.policy.feedback --to-golden     # candidate golden rows as JSONL
    uv run python -m nw.policy.feedback --to-golden --all   # helpful answers too, as positives

Each record carries `submitted_by`, the key id of whoever sent it, so a flood of verdicts
from one key is visible and a candidate can be traced to its source before anyone trusts
it. With `NW_OPS_STORE` set the verdicts live in the platform's object storage under the
tenant's `feedback/` prefix, and this module reads them from there.

Nothing here writes to `data/golden/`. A person reads each candidate, fixes `gold_answer`
and `gold_chunk_ids`, decides `must_refuse`, and commits it. The harness then runs it on
every pull request, which is how the same mistake is caught the second time before a
customer sees it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from nw.policy.cache import normalise

DEFAULT_PATH = Path("artifacts/policy/feedback.jsonl")
Verdict = Literal["helpful", "wrong", "unsafe"]
VERDICTS: tuple[str, ...] = ("helpful", "wrong", "unsafe")


class FeedbackIn(BaseModel):
    answer_id: str = Field(min_length=8, max_length=64)
    verdict: Verdict
    note: str = Field(default="", max_length=2000)


def append_feedback(target: Path | Any, record: dict[str, Any], *, key: str | None = None) -> None:
    """Append one verdict to a JSONL file, or to an ops store (`nw.agent.opstore`) at `key`."""
    line = {"ts": time.time(), **record}
    if isinstance(target, Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        return
    target.append(key or "verdicts", line)


def load_feedback(path: Path) -> list[dict[str, Any]]:
    """The verdicts: from the ops store when `NW_OPS_STORE` is set, else the JSONL file."""
    import os

    if os.environ.get("NW_OPS_STORE"):
        from nw.agent.opstore import store_for

        return store_for("feedback", local=path.parent).records("verdicts")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def must_refuse_guess(record: dict[str, Any]) -> bool:
    """`unsafe` means the service should have refused, whatever it did. `wrong` on a refusal
    means it should have answered; `wrong` on an answer means the answer needs correcting,
    and the question stays answerable. A person confirms every guess."""
    return record["verdict"] == "unsafe"


def to_golden(records: list[dict[str, Any]], *, include_helpful: bool = False) -> list[dict]:
    """Candidate rows in the golden set's shape, one per distinct question, newest verdict
    wins. `gold_answer` is left empty on wrong answers: that is the human's job."""
    keep = set(VERDICTS) if include_helpful else {"wrong", "unsafe"}
    by_question: dict[str, dict[str, Any]] = {}
    for r in records:
        if r.get("verdict") in keep and r.get("question"):
            by_question[normalise(r["question"])] = r
    rows = []
    for r in by_question.values():
        qid = hashlib.sha256(normalise(r["question"]).encode()).hexdigest()[:12]
        helpful = r["verdict"] == "helpful"
        rows.append(
            {
                "id": f"fb-{qid}",
                "question": r["question"],
                "gold_answer": r.get("text", "") if helpful else "",
                "gold_chunk_ids": list(r.get("citations") or []),
                "must_refuse": must_refuse_guess(r),
                "audience": r.get("audience", "customer"),
                "source": {
                    "verdict": r["verdict"],
                    "submitted_by": r.get("submitted_by"),
                    "note": r.get("note", ""),
                    "answer_id": r.get("answer_id"),
                    "prompt_version": r.get("prompt_version"),
                    "refused": r.get("refused"),
                },
            }
        )
    return sorted(rows, key=lambda x: x["id"])


def format_summary(records: list[dict[str, Any]]) -> str:
    counts = {v: sum(1 for r in records if r.get("verdict") == v) for v in VERDICTS}
    lines = [f"{len(records)} verdicts: " + ", ".join(f"{v} {n}" for v, n in counts.items())]
    for r in records:
        if r.get("verdict") != "helpful":
            q = (r.get("question") or "")[:70]
            lines.append(f"  {r['verdict']:7s} {q!r}  {r.get('note', '')[:60]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--to-golden", action="store_true", help="print candidate golden rows as JSONL")
    ap.add_argument("--all", action="store_true", help="include helpful answers as positives")
    args = ap.parse_args(argv)
    records = load_feedback(args.path)
    if args.to_golden:
        for row in to_golden(records, include_helpful=args.all):
            print(json.dumps(row))
    else:
        print(format_summary(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
