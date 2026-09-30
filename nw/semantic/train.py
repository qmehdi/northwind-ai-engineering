"""Fine-tune the encoder with LoRA: mixed precision, gradient accumulation,
clipping, resumable checkpoints, per-label thresholds, and a versioned artifact.

    uv run python -m nw.semantic.train --data data/tickets.jsonl --out artifacts/semantic \
        --subset 2000 --epochs 6 --lr 1e-3 --no-promote

The loop is written out rather than hidden behind a Trainer so a NaN, a
deadlocked dataloader or an out-of-memory can be read off the code.

Around the loop sits the same operational shape as Project 1: the data passes its
contract before anything trains, every run writes `artifacts/semantic/<version>/` with
the data profile, the validation and test evaluation, a model card and a line in
`runs.jsonl`. Without `--no-promote` the run continues into the export, the benchmark
against Project 1 and the promotion gate, which is the only thing that moves `latest`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import torch
from sklearn.metrics import f1_score, recall_score
from torch import nn
from torch.utils.data import DataLoader

from nw.semantic.artifacts import newest_candidate
from nw.semantic.data import TAGS, load_rows, make_loader, stratified_subset
from nw.semantic.model import (
    ModelSpec,
    TicketEncoder,
    apply_lora,
    build_encoder,
    count_parameters,
    pick_device,
)
from nw.triage.data_check import profile as profile_data
from nw.triage.data_check import validate
from nw.triage.features import PRIORITIES
from nw.triage.train import data_hash, git_sha


def loss_fn(
    tag_logits: torch.Tensor,
    prio_logits: torch.Tensor,
    batch: dict[str, torch.Tensor],
    prio_weight: torch.Tensor | None,
) -> torch.Tensor:
    """Multi-label BCE on tags plus weighted cross-entropy on priority. BCE treats each
    tag as its own yes/no question, which is what multi-label means; softmax would
    force the tags to compete."""
    # SOLUTION BEGIN
    tag_loss = nn.functional.binary_cross_entropy_with_logits(tag_logits, batch["tags"])
    prio_loss = nn.functional.cross_entropy(prio_logits, batch["priority"], weight=prio_weight)
    return tag_loss + prio_loss
    # STUB: raise NotImplementedError("The loss: BCE for tags, weighted CE for priority")
    # SOLUTION END


@torch.no_grad()
def predict(
    model: TicketEncoder, loader: DataLoader, device: torch.device
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    model.eval()
    tag_p, prio_p, tag_y, prio_y = [], [], [], []
    for batch in loader:
        ids, mask = batch["input_ids"].to(device), batch["attention_mask"].to(device)
        tl, pl = model(ids, mask)
        tag_p.append(torch.sigmoid(tl).float().cpu().numpy())
        prio_p.append(torch.softmax(pl, -1).float().cpu().numpy())
        tag_y.append(batch["tags"].numpy())
        prio_y.append(batch["priority"].numpy())
    return (
        np.concatenate(tag_p),
        np.concatenate(prio_p),
        np.concatenate(tag_y),
        np.concatenate(prio_y),
    )


def tune_tag_thresholds(prob: np.ndarray, y: np.ndarray) -> np.ndarray:
    """One threshold per tag, chosen on validation for F1. A global 0.5 under-predicts
    rare tags; the per-label threshold is the cheapest large win in multi-label work."""
    # SOLUTION BEGIN
    thresholds = np.full(prob.shape[1], 0.5, dtype=np.float32)
    grid = np.linspace(0.1, 0.9, 17)
    for j in range(prob.shape[1]):
        if y[:, j].sum() == 0:
            continue
        scores = [f1_score(y[:, j], prob[:, j] >= t, zero_division=0) for t in grid]
        thresholds[j] = grid[int(np.argmax(scores))]
    return thresholds
    # STUB: return np.full(prob.shape[1], 0.5, dtype=np.float32)  # per-tag thresholds
    # SOLUTION END


def metrics(
    tag_p: np.ndarray,
    prio_p: np.ndarray,
    tag_y: np.ndarray,
    prio_y: np.ndarray,
    thresholds: np.ndarray,
) -> dict[str, Any]:
    tag_pred = tag_p >= thresholds
    prio_pred = prio_p.argmax(1)
    labelled = prio_y >= 0
    return {
        "tag_micro_f1": float(f1_score(tag_y, tag_pred, average="micro", zero_division=0)),
        "tag_macro_f1": float(f1_score(tag_y, tag_pred, average="macro", zero_division=0)),
        "priority_macro_f1": float(
            f1_score(
                prio_y[labelled],
                prio_pred[labelled],
                average="macro",
                labels=list(range(len(PRIORITIES))),
                zero_division=0,
            )
        ),
        "p0_recall": float(
            recall_score(prio_y[labelled] == 0, prio_pred[labelled] == 0, zero_division=0)
        ),
    }


def save_checkpoint(
    path: Path,
    model: TicketEncoder,
    optimizer: torch.optim.Optimizer,
    scheduler: Any,
    epoch: int,
    step: int,
    best: float,
) -> None:
    """Everything needed to resume: adapter and head weights, optimiser moments,
    scheduler position, where we were, and the best score so far."""
    # SOLUTION BEGIN
    torch.save(
        {
            "model": {k: v for k, v in model.state_dict().items() if "lora" in k or "head" in k},
            "optimizer": optimizer.state_dict(),
            "scheduler": scheduler.state_dict(),
            "epoch": epoch,
            "step": step,
            "best": best,
        },
        path,
    )
    # STUB: torch.save({"model": model.state_dict()}, path)  # resume: what else does it need?
    # SOLUTION END


def load_checkpoint(
    path: Path,
    model: TicketEncoder,
    optimizer: torch.optim.Optimizer | None = None,
    scheduler: Any = None,
) -> dict[str, Any]:
    # weights_only: tensors and plain containers, never arbitrary pickled objects, so a
    # checkpoint from a shared bucket cannot run code when it is resumed.
    ckpt = torch.load(path, map_location="cpu", weights_only=True)
    missing, unexpected = model.load_state_dict(ckpt["model"], strict=False)
    if unexpected:
        raise ValueError(f"checkpoint has unexpected keys: {unexpected[:3]}")
    if optimizer is not None and "optimizer" in ckpt:
        optimizer.load_state_dict(ckpt["optimizer"])
    if scheduler is not None and "scheduler" in ckpt:
        scheduler.load_state_dict(ckpt["scheduler"])
    return ckpt


def evaluate_rows(
    model: TicketEncoder,
    rows: list[dict[str, Any]],
    tokenizer: Any,
    thresholds: np.ndarray,
    device: torch.device,
    *,
    batch_size: int = 32,
    max_length: int = 256,
) -> dict[str, Any]:
    """The four metrics on `rows`, then the same per language. A language with no P0
    rows reports `p0_recall` as None rather than a misleading zero."""
    loader = make_loader(rows, tokenizer, batch_size=batch_size, max_length=max_length)
    tag_p, prio_p, tag_y, prio_y = predict(model, loader, device)
    out = {"n": len(rows), **metrics(tag_p, prio_p, tag_y, prio_y, thresholds)}
    langs = np.asarray([r.get("language", "en") for r in rows])
    by_language: dict[str, Any] = {}
    for code in sorted(set(langs)):
        m = langs == code
        sub = metrics(tag_p[m], prio_p[m], tag_y[m], prio_y[m], thresholds)
        if not (prio_y[m] == 0).any():
            sub["p0_recall"] = None
        by_language[code] = {"n": int(m.sum()), **sub}
    out["by_language"] = by_language
    # What the model predicts on these rows, the reference the service's drift monitor compares
    # live predictions with (`predicted_share`, `predicted_tag_share` in the data profile).
    prio_pred = prio_p.argmax(1)
    tag_pred = tag_p >= thresholds
    out["predicted_share"] = {
        p: float((prio_pred == i).mean()) if len(prio_pred) else 0.0
        for i, p in enumerate(PRIORITIES)
    }
    out["predicted_tag_share"] = {
        t: float(tag_pred[:, i].mean()) if len(tag_pred) else 0.0 for i, t in enumerate(TAGS)
    }
    return out


def find_checkpoint(path: Path) -> Path:
    """`--resume` takes a checkpoint file, a version directory, or an artifact root, in
    which case the newest candidate's checkpoint is meant."""
    if path.is_file():
        return path
    # A run stopped with Ctrl-C has a checkpoint but no metadata.json yet: a directory holding
    # a checkpoint is the one to resume, finished or not.
    if (path / "checkpoint.pt").exists():
        return path / "checkpoint.pt"
    if path.is_dir():
        runs = [
            d
            for d in path.iterdir()
            if d.is_dir() and not d.is_symlink() and (d / "checkpoint.pt").exists()
        ]
        if runs:
            return max(runs, key=lambda d: (d / "checkpoint.pt").stat().st_mtime) / "checkpoint.pt"
    return newest_candidate(path) / "checkpoint.pt"


def tag_share(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Share of rows carrying each tag: the training prevalence the service measures
    the tag rate of live traffic against."""
    n = max(len(rows), 1)
    counts = {t: 0 for t in TAGS}
    for r in rows:
        for t in r.get("tags", []):
            if t in counts:
                counts[t] += 1
    return {t: c / n for t, c in counts.items()}


def train(
    data: Path,
    out: Path,
    *,
    spec: ModelSpec | None = None,
    epochs: int = 2,
    batch_size: int = 16,
    accumulate: int = 2,
    lr: float = 3e-4,
    subset: int | None = None,
    resume: Path | None = None,
    device: torch.device | None = None,
    config: Any = None,
    tokenizer: Any = None,
    max_steps: int | None = None,
    log_every: int = 20,
    seed: int = 0,
) -> tuple[Path, dict[str, Any]]:
    """Validate the data, train, evaluate on validation and test, write the versioned
    artifact under `out/<version>/` with its data profile and model card, and record the
    run. Promotion is a separate step: the gate needs the export and the benchmark."""
    spec = spec or ModelSpec()
    device = device or pick_device()
    # Seed everything the loop touches: adapter init, dropout, shuffling. A run that
    # cannot be repeated cannot be debugged.
    torch.manual_seed(seed)
    np.random.seed(seed)
    rows = load_rows(data)
    findings = validate(rows)
    blocking = [f for f in findings if f.blocking]
    if blocking:
        raise SystemExit(
            "data check failed: " + "; ".join(f"{f.check}: {f.detail}" for f in blocking)
        )
    sha = data_hash(data)
    data_profile = asdict(profile_data(rows, sha, findings))
    rows_train = [r for r in rows if r.get("split") == "train"]
    rows_val = [r for r in rows if r.get("split") == "val"]
    rows_test = [r for r in rows if r.get("split") == "test"]
    if subset:
        rows_train = stratified_subset(rows_train, subset)
    data_profile["tag_share"] = tag_share(rows_train)
    model, tok = build_encoder(spec, config=config)
    tokenizer = tokenizer or tok
    model = apply_lora(model, spec).to(device)
    total, trainable = count_parameters(model)
    print(f"parameters: {total:,} total, {trainable:,} trainable ({100 * trainable / total:.2f}%)")

    train_loader = make_loader(
        rows_train, tokenizer, batch_size=batch_size, shuffle=True, max_length=spec.max_length
    )
    val_loader = make_loader(
        rows_val, tokenizer, batch_size=batch_size * 2, max_length=spec.max_length
    )

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=lr, weight_decay=0.01)
    steps_per_epoch = math.ceil(len(train_loader) / accumulate)
    total_steps = steps_per_epoch * epochs
    warmup = max(1, int(0.06 * total_steps))
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lambda s: (
            min(1.0, (s + 1) / warmup) * max(0.0, (total_steps - s) / max(1, total_steps - warmup))
        ),
    )

    # One directory per run: the minute, the code and the data it was trained on. A
    # resumed run is a new version that names the checkpoint it continued from.
    version = f"{dt.datetime.now(dt.UTC).strftime('%Y%m%d%H%M%S')}-{git_sha()}-{sha}"
    run_dir = out / version
    run_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = run_dir / "checkpoint.pt"
    start_epoch, step, best = 0, 0, -1.0
    resumed_from: str | None = None
    if resume:
        resume = find_checkpoint(resume)
        ck = load_checkpoint(resume, model, optimizer, scheduler)
        start_epoch, step = ck["epoch"], ck["step"]
        resumed_from = str(resume)
        # Best is per version: the new directory always gets a best.pt of its own.
        print(f"resumed from {resume}: epoch {start_epoch}, step {step}, best {ck['best']:.4f}")

    # Loop invariants, given. Priority weights are inverse class frequency, so a P0
    # mistake costs the loss more than a P2 mistake. fp16 gradients underflow, so on
    # CUDA the loss is scaled before backward and unscaled before the step; elsewhere
    # the scaler is disabled and passes through.
    counts = np.bincount(
        [PRIORITIES.index(r["priority"]) for r in rows_train], minlength=len(PRIORITIES)
    ).astype(np.float32)
    prio_weight = torch.tensor(  # noqa: F841 (used in the loop the skeleton stubs)
        counts.sum() / np.maximum(counts, 1) / len(PRIORITIES), dtype=torch.float32
    ).to(device)
    use_amp = device.type == "cuda"
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)  # noqa: F841 (same)

    history: list[dict[str, Any]] = []
    started = time.perf_counter()
    for epoch in range(start_epoch, epochs):
        model.train()
        # SOLUTION BEGIN
        running = 0.0
        for i, batch in enumerate(train_loader):
            ids, mask = batch["input_ids"].to(device), batch["attention_mask"].to(device)
            target = {k: batch[k].to(device) for k in ("tags", "priority")}
            with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=use_amp):
                tl, pl = model(ids, mask)
                loss = loss_fn(tl, pl, target, prio_weight) / accumulate
            scaler.scale(loss).backward()
            running += loss.item() * accumulate
            if (i + 1) % accumulate == 0 or i + 1 == len(train_loader):
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad(set_to_none=True)
                scheduler.step()
                step += 1
                if step % log_every == 0:
                    print(
                        f"epoch {epoch} step {step}/{total_steps} loss {running / log_every:.4f} "
                        f"lr {scheduler.get_last_lr()[0]:.2e} {time.perf_counter() - started:.0f}s"
                    )
                    running = 0.0
                if max_steps and step >= max_steps:
                    break
            if not math.isfinite(loss.item()):
                raise RuntimeError(
                    f"loss is {loss.item()} at step {step}: lower the learning rate or check data"
                )
        # STUB: raise NotImplementedError("The training step: the batch loop")
        # SOLUTION END
        tag_p, prio_p, tag_y, prio_y = predict(model, val_loader, device)
        thresholds = tune_tag_thresholds(tag_p, tag_y)
        m = metrics(tag_p, prio_p, tag_y, prio_y, thresholds)
        history.append({"epoch": epoch, **m})
        score = m["tag_micro_f1"] + m["priority_macro_f1"]
        print(f"epoch {epoch} val: {json.dumps({k: round(v, 4) for k, v in m.items()})}")
        if score > best:
            best = score
            save_checkpoint(run_dir / "best.pt", model, optimizer, scheduler, epoch + 1, step, best)
            np.save(run_dir / "tag_thresholds.npy", thresholds)
            (run_dir / "best_metrics.json").write_text(json.dumps({"epoch": epoch, **m}, indent=1))
        save_checkpoint(ckpt_path, model, optimizer, scheduler, epoch + 1, step, best)
        if max_steps and step >= max_steps:
            break
    seconds = round(time.perf_counter() - started, 1)

    # The artifact is the best epoch, so the evaluation is of the best epoch: reload it,
    # then score validation and test with the thresholds chosen on validation.
    load_checkpoint(run_dir / "best.pt", model)
    thresholds = np.load(run_dir / "tag_thresholds.npy")
    evaluation = {
        split: evaluate_rows(
            model,
            split_rows,
            tokenizer,
            thresholds,
            device,
            batch_size=batch_size * 2,
            max_length=spec.max_length,
        )
        for split, split_rows in (("val", rows_val), ("test", rows_test))
    }
    # The validation predictions become the drift monitor's reference; they stay out of the
    # metrics so the model card and the gate read the same numbers as before.
    for split in ("val", "test"):
        shares = (
            evaluation[split].pop("predicted_share"),
            evaluation[split].pop("predicted_tag_share"),
        )
        if split == "val":
            data_profile["predicted_share"], data_profile["predicted_tag_share"] = shares
            data_profile["predicted_share_source"] = f"validation predictions of {version}"
    print(
        "test: "
        + json.dumps(
            {k: round(v, 4) for k, v in evaluation["test"].items() if isinstance(v, float)}
        )
    )

    meta = {
        "version": version,
        "trained_at": dt.datetime.now(dt.UTC).isoformat(),
        "data": str(data),
        "data_sha256_12": sha,
        "git_sha": git_sha(),
        "base": spec.base,
        "lora": {
            "r": spec.lora_r,
            "alpha": spec.lora_alpha,
            "dropout": spec.lora_dropout,
            "targets": list(spec.target_modules),
        },
        "max_length": spec.max_length,
        "tags": TAGS,
        "priorities": PRIORITIES,
        "parameters_total": total,
        "parameters_trainable": trainable,
        "n_train": len(rows_train),
        "n_val": len(rows_val),
        "n_test": len(rows_test),
        "subset": subset,
        "epochs": epochs,
        "batch_size": batch_size,
        "accumulate": accumulate,
        "lr": lr,
        "device": device.type,
        "seed": seed,
        "seconds": seconds,
        "resumed_from": resumed_from,
        "history": history,
        "best_metrics": json.loads((run_dir / "best_metrics.json").read_text()),
        "metrics": evaluation,
    }
    (run_dir / "metadata.json").write_text(json.dumps(meta, indent=1))
    (run_dir / "data_profile.json").write_text(json.dumps(data_profile, indent=1))
    from nw.semantic.model_card import write as write_card
    from nw.semantic.tracking import record_run

    write_card(run_dir)
    meta["run"] = record_run(out, meta)
    return run_dir, meta


def export_benchmark_gate(
    out: Path, run_dir: Path, data: Path, triage: Path, *, force: bool = False
) -> bool:
    """The rest of the loop after training: export and parity, the benchmark against
    Project 1, then the gate. Each step writes into the version directory the next one
    reads. Returns whether the candidate was promoted."""
    from nw.semantic.benchmark import run as run_benchmark
    from nw.semantic.benchmark import write as write_benchmark
    from nw.semantic.export import run as run_export
    from nw.semantic.promote import format_decision, promote

    print(f"\nexport {run_dir}")
    report = run_export(run_dir, data)
    print(json.dumps(report, indent=1))
    if not (triage / "metadata.json").exists():
        raise SystemExit(
            f"benchmark needs Project 1 at {triage}: run `make train-triage` first, "
            "or pass --triage <artifact>"
        )
    print(f"\nbenchmark against {triage}")
    results = run_benchmark(triage, run_dir, data)
    write_benchmark(run_dir, results)
    from nw.semantic.benchmark import format_table

    print(format_table(results))
    print()
    decision = promote(
        out, run_dir.name, force=force, write_summary=data == Path("data/tickets.jsonl")
    )
    print(format_decision(decision))
    return decision.passed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/semantic"))
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument(
        "--subset", type=int, default=None, help="stratified training subset for laptops"
    )
    ap.add_argument(
        "--resume", type=Path, default=None, help="a checkpoint.pt, a version directory or a root"
    )
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--no-promote",
        action="store_true",
        help="stop after training: register the candidate, skip export, benchmark and the gate",
    )
    ap.add_argument(
        "--triage",
        type=Path,
        default=Path("artifacts/triage/latest"),
        help="the Project 1 artifact the benchmark compares against",
    )
    ap.add_argument(
        "--force", action="store_true", help="promote despite a failed gate, recorded as forced"
    )
    args = ap.parse_args()
    run_dir, meta = train(
        args.data,
        args.out,
        epochs=args.epochs,
        batch_size=args.batch_size,
        subset=args.subset,
        resume=args.resume,
        lr=args.lr,
        seed=args.seed,
    )
    print(json.dumps({k: v for k, v in meta.items() if k not in ("tags", "history")}, indent=1))
    if args.no_promote:
        print(
            f"\ncandidate {meta['version']} registered, not promoted: "
            "`make export-semantic`, `make benchmark`, then `make promote-semantic`"
        )
        return 0
    return (
        0
        if export_benchmark_gate(args.out, run_dir, args.data, args.triage, force=args.force)
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
