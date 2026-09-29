"""Project 1, step 1: the data contract, before anything trains.

    python -m nw.pipelines.steps.triage_data_check --data data/tickets.jsonl --out artifacts/triage

Runs `nw.triage.data_check` and writes the profile and the findings under the artifact root.
Exit 1 on a blocking finding, so the pipeline stops here and not three steps later.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nw.pipelines.steps import localize, write_json, write_result
from nw.triage.data_check import check_file, format_profile

STEP = "triage_data_check"


def run(data: Path, out: Path) -> dict[str, Any]:
    profile = check_file(localize(data))
    write_json(Path(out) / "data_profile.json", asdict(profile))
    result = {
        "step": STEP,
        "ok": profile.ok,
        "n": profile.n,
        "data_sha256_12": profile.data_sha256_12,
        "splits": profile.splits,
        "findings": profile.findings,
    }
    write_json(Path(out) / "data_check.json", result)
    write_result(out, STEP, result)
    print(format_profile(profile))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/triage"))
    args = ap.parse_args(argv)
    return 0 if run(args.data, args.out)["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
