"""Project 2, step 2: fine-tune a candidate.

    python -m nw.pipelines.steps.semantic_train --data data/tickets.jsonl --out artifacts/semantic \
        --subset 2000 --epochs 2 --lr 1e-3

`nw.semantic.train.train` as `make train-semantic` calls it: a versioned candidate with its
profile, card and run record, no export and no gate. Those are the next steps. `config` and
`tokenizer` exist so the tests can train the tiny random encoder; the command line trains
the real base.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import torch

from nw.pipelines.steps import localize, write_result
from nw.semantic.model import ModelSpec
from nw.semantic.train import train

STEP = "semantic_train"


def run(
    data: Path,
    out: Path,
    *,
    epochs: int = 2,
    batch_size: int = 16,
    accumulate: int = 2,
    lr: float = 1e-3,
    subset: int | None = 2000,
    seed: int = 0,
    spec: ModelSpec | None = None,
    device: torch.device | None = None,
    config: Any = None,
    tokenizer: Any = None,
    log_every: int = 20,
) -> dict[str, Any]:
    run_dir, meta = train(
        localize(data),
        Path(out),
        spec=spec,
        epochs=epochs,
        batch_size=batch_size,
        accumulate=accumulate,
        lr=lr,
        subset=subset or None,
        seed=seed,
        device=device,
        config=config,
        tokenizer=tokenizer,
        log_every=log_every,
    )
    test = meta["metrics"]["test"]
    result = {
        "step": STEP,
        "version": meta["version"],
        "artifact": str(run_dir),
        "data_sha256_12": meta["data_sha256_12"],
        "seconds": meta["seconds"],
        "device": meta["device"],
        "metrics": {
            k: test[k] for k in ("tag_micro_f1", "tag_macro_f1", "priority_macro_f1", "p0_recall")
        },
    }
    write_result(out, STEP, result)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/semantic"))
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--accumulate", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--subset", type=int, default=2000, help="0 means the whole training split")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--base", default=None, help="the base encoder; default is the course base")
    ap.add_argument("--max-length", type=int, default=None)
    args = ap.parse_args(argv)
    spec = None
    if args.base or args.max_length:
        default = ModelSpec()
        spec = ModelSpec(
            base=args.base or default.base, max_length=args.max_length or default.max_length
        )
    result = run(
        args.data,
        args.out,
        epochs=args.epochs,
        batch_size=args.batch_size,
        accumulate=args.accumulate,
        lr=args.lr,
        subset=args.subset,
        seed=args.seed,
        spec=spec,
    )
    print(f"candidate {result['version']} in {result['seconds']}s on {result['device']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
