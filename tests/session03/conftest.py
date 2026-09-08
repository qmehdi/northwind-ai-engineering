"""Session 3 tests run on a tiny random DistilBERT so they need no download and finish
on CPU in seconds. The tokenizer is a small word-piece vocab built here."""

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

    rows = make_rows(400, seed=3)
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
