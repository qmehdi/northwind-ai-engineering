"""Project 2, step 1: the data contract plus what the encoder needs on top of it.

    python -m nw.pipelines.steps.semantic_data_prep --data data/tickets.jsonl --out <root>

Project 1's contract (schema, label set, splits, P0 share, duplicates, leaks) and then the
tags: every training row needs at least one tag from the fixed vocabulary, and a tag the
vocabulary does not know is a finding, because the tag head cannot learn it. The profile
carries the training tag share the service measures drift against. Exit 1 on a blocking
finding.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nw.pipelines.steps import local_path, localize, write_json, write_result
from nw.semantic.data import TAG_INDEX, load_rows
from nw.semantic.train import tag_share
from nw.triage.data_check import Finding, format_profile, profile, validate

STEP = "semantic_data_prep"
MIN_TAGGED_SHARE = 0.95


def tag_findings(rows: list[dict[str, Any]]) -> list[Finding]:
    out: list[Finding] = []
    train = [r for r in rows if r.get("split") == "train"]
    if not train:
        return [Finding("tags", "no training rows")]
    untagged = sum(1 for r in train if not r.get("tags"))
    share = 1 - untagged / len(train)
    if share < MIN_TAGGED_SHARE:
        out.append(
            Finding(
                "tags", f"{share:.1%} of training rows carry a tag; need {MIN_TAGGED_SHARE:.0%}"
            )
        )
    unknown = Counter(t for r in rows for t in r.get("tags", []) if t not in TAG_INDEX)
    if unknown:
        out.append(
            Finding(
                "tag_vocabulary",
                f"{sum(unknown.values())} tag uses outside the vocabulary: "
                + ", ".join(f"{k} ({v})" for k, v in unknown.most_common(5)),
                blocking=False,
            )
        )
    return out


def run(data: Path | str, out: Path | str) -> dict[str, Any]:
    out = local_path(out)
    path = localize(data)
    rows = load_rows(path)
    findings = validate(rows) + tag_findings(rows)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    prof = profile(rows, sha, findings)
    payload = asdict(prof)
    payload["tag_share"] = tag_share([r for r in rows if r.get("split") == "train"])
    write_json(Path(out) / "data_profile.json", payload)
    result = {
        "step": STEP,
        "ok": prof.ok,
        "n": prof.n,
        "data_sha256_12": sha,
        "splits": prof.splits,
        "tags_in_training": len([t for t, s in payload["tag_share"].items() if s > 0]),
        "findings": prof.findings,
    }
    write_json(Path(out) / "data_check.json", result)
    write_result(out, STEP, result)
    print(format_profile(prof))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="data/tickets.jsonl", help="a path, gs:// or s3:// URI")
    ap.add_argument(
        "--out", default="artifacts/semantic", help="the artifact tree: a path or gs:// URI"
    )
    args = ap.parse_args(argv)
    return 0 if run(args.data, args.out)["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
