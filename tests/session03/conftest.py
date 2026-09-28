"""The deep learning tests run on a tiny random DistilBERT so they need no download and
finish on CPU in seconds. The tokenizer is a small word-piece vocab built here. The
fixture corpus is 600 rows so it passes the data contract the trainer enforces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import torch

from nw.semantic.model import ModelSpec, tiny_config

torch.manual_seed(0)


@pytest.fixture(scope="session")
def tiny_tokenizer():
    from tokenizers import Tokenizer, models, pre_tokenizers, trainers
    from transformers import PreTrainedTokenizerFast

    tok = Tokenizer(models.WordPiece(unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    trainer = trainers.WordPieceTrainer(
        vocab_size=2000, special_tokens=["[PAD]", "[UNK]", "[CLS]", "[SEP]"]
    )
    corpus = [
        "production down outage all users critical error 502 breach security data loss",
        "sso login failed redirect loop saml users major impact webhook timeout",
        "slow dashboard duplicate alert email invoice seats not urgent",
        "how to export audit log documentation feature request dark mode question plan limits",
    ] * 50
    tok.train_from_iterator(corpus, trainer)
    fast = PreTrainedTokenizerFast(
        tokenizer_object=tok,
        pad_token="[PAD]",
        unk_token="[UNK]",
        cls_token="[CLS]",
        sep_token="[SEP]",
    )
    return fast


@pytest.fixture(scope="session")
def spec():
    return ModelSpec(base="", max_length=32)


@pytest.fixture(scope="session")
def config():
    cfg = tiny_config()
    cfg.vocab_size = 2100
    return cfg


@pytest.fixture(scope="session")
def s3_rows():
    from tests.session02.conftest import make_rows

    rows = make_rows(600, seed=8)  # 600 rows and P0 in every split: the data contract
    tag_for = {
        "P0": ["Outage", "Security"],
        "P1": ["SSO", "Webhook"],
        "P2": ["Dashboard", "Duplicate"],
        "P3": ["Documentation", "Feature"],
    }
    for r in rows:
        r["tags"] = tag_for[r["priority"]] + (["Bug"] if r["priority"] in ("P1", "P2") else [])
    return rows


@pytest.fixture(scope="session")
def s3_file(tmp_path_factory, s3_rows) -> Path:
    p = tmp_path_factory.mktemp("s3") / "tickets.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in s3_rows))
    return p


@pytest.fixture(scope="session")
def trained_tiny(tmp_path_factory, s3_file, spec, config, tiny_tokenizer):
    from nw.semantic.train import train

    out = tmp_path_factory.mktemp("semantic")
    out, meta = train(
        s3_file,
        out,
        spec=spec,
        epochs=6,
        batch_size=16,
        accumulate=1,
        lr=1e-2,
        device=torch.device("cpu"),
        config=config,
        tokenizer=tiny_tokenizer,
        log_every=1000,
    )
    return out, meta


@pytest.fixture(scope="session")
def exported_tiny(trained_tiny, s3_file, config, tiny_tokenizer):
    """The trained candidate after the export step: both graphs, the tokenizer and the
    export report in the version directory."""
    from nw.semantic.export import run as run_export

    out, _ = trained_tiny
    report = run_export(out, s3_file, 5, config=config, tokenizer=tiny_tokenizer)
    return out, report


@pytest.fixture(scope="session")
def benchmarked_tiny(exported_tiny, s3_file, tmp_path_factory, config, tiny_tokenizer):
    """The candidate after the benchmark: a Project 1 model trained on the same fixture,
    the four-row table written to benchmark.json."""
    from nw.semantic.benchmark import run as run_benchmark
    from nw.semantic.benchmark import write as write_benchmark
    from nw.triage.train import train as train_triage

    out, _ = exported_tiny
    triage_root = tmp_path_factory.mktemp("triage")
    triage_model, _ = train_triage(s3_file, triage_root, promote=False)
    results = run_benchmark(
        triage_root / triage_model.version,
        out,
        s3_file,
        latency_n=5,
        config=config,
        tokenizer=tiny_tokenizer,
    )
    write_benchmark(out, results)
    return out, results
