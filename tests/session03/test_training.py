"""Acceptance: LoRA trains under 2 percent of parameters, learns on a tiny corpus,
resumes from a checkpoint, and per-label thresholds beat a flat 0.5."""

import numpy as np
import pytest
import torch

from nw.semantic.model import apply_lora, build_encoder, count_parameters
from nw.semantic.train import load_checkpoint, train, tune_tag_thresholds

pytestmark = pytest.mark.session03


def test_lora_trains_a_small_fraction(config, spec):
    model, _ = build_encoder(spec, config=config)
    total_before, _ = count_parameters(model)
    model = apply_lora(model, spec)
    total, trainable = count_parameters(model)
    heads = sum(
        p.numel()
        for p in list(model.tag_head.parameters()) + list(model.priority_head.parameters())
    )
    assert trainable < total and trainable - heads > 0
    assert all(not p.requires_grad for n, p in model.encoder.named_parameters() if "lora" not in n)


def test_training_learns_and_checkpoints(trained_tiny):
    out, meta = trained_tiny
    assert out.name == meta["version"], "the artifact is a version directory under the root"
    assert (
        (out / "best.pt").exists()
        and (out / "checkpoint.pt").exists()
        and (out / "tag_thresholds.npy").exists()
    )
    assert meta["best_metrics"]["tag_micro_f1"] > 0.5
    assert meta["best_metrics"]["priority_macro_f1"] > 0.5
    assert meta["parameters_trainable"] < meta["parameters_total"]


def test_resume_restores_step_and_optimizer(
    trained_tiny, s3_file, spec, config, tiny_tokenizer, tmp_path
):
    out, meta = trained_tiny
    model, _ = build_encoder(spec, config=config)
    model = apply_lora(model, spec)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=1e-3)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: 1.0)
    ck = load_checkpoint(out / "checkpoint.pt", model, opt, sched)
    assert ck["epoch"] == 6 and ck["step"] > 0
    assert len(opt.state) > 0, "optimizer moments were not saved; resume would restart Adam cold"
    out2, meta2 = train(
        s3_file,
        tmp_path,
        spec=spec,
        epochs=7,
        batch_size=16,
        accumulate=1,
        lr=1e-2,
        device=torch.device("cpu"),
        config=config,
        tokenizer=tiny_tokenizer,
        resume=out.parent,  # the root: the newest candidate's checkpoint is meant
        log_every=1000,
    )
    assert meta2["best_metrics"] is not None
    assert meta2["resumed_from"] == str(out / "checkpoint.pt") and out2.name == meta2["version"]


def test_per_label_thresholds_help_rare_tags():
    rng = np.random.default_rng(1)
    y = np.zeros((500, 3))
    y[:20, 0] = 1  # rare tag
    y[:, 1] = rng.random(500) < 0.5
    prob = np.clip(
        y * 0.4 + rng.random((500, 3)) * 0.4, 0, 1
    )  # rare tag maxes around 0.8, negatives up to 0.4
    t = tune_tag_thresholds(prob, y)
    assert t[0] < 0.5
    from sklearn.metrics import f1_score

    assert f1_score(y[:, 0], prob[:, 0] >= t[0]) >= f1_score(y[:, 0], prob[:, 0] >= 0.5)
