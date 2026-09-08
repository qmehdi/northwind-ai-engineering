"""Acceptance: ONNX export matches PyTorch within 1e-3, quantisation shrinks the file,
and the exported graph accepts variable batch and sequence sizes."""

import numpy as np
import pytest

from nw.semantic.export import OnnxEncoder, export_onnx, load_finetuned, parity, quantize

pytestmark = pytest.mark.session03


@pytest.fixture(scope="session")
def exported(trained_tiny, config, tiny_tokenizer):
    out, _ = trained_tiny
    model, tok, meta = load_finetuned(out, config=config, tokenizer=tiny_tokenizer)
    fp32 = export_onnx(model, tok, out / "model.onnx", meta["max_length"])
    int8 = quantize(fp32, out / "model.int8.onnx")
    return model, tok, meta, fp32, int8


def test_fp32_parity_within_1e_3(exported):
    model, tok, meta, fp32, _ = exported
    texts = [
        "production down outage all users",
        "how to export audit log documentation",
        "slow dashboard duplicate alert",
    ]
    assert (
        parity(model, OnnxEncoder(fp32, tok, meta["max_length"]), tok, texts, meta["max_length"])
        < 1e-3
    )


def test_int8_is_smaller_and_close(exported):
    model, tok, meta, fp32, int8 = exported
    assert int8.stat().st_size < fp32.stat().st_size
    texts = ["sso login failed redirect loop saml"]
    assert (
        parity(model, OnnxEncoder(int8, tok, meta["max_length"]), tok, texts, meta["max_length"])
        < 0.5
    )


def test_dynamic_axes(exported):
    _, tok, meta, fp32, _ = exported
    onnx = OnnxEncoder(fp32, tok, meta["max_length"])
    a, b, e = onnx.run(["short", "a much longer ticket text about an outage with many words in it"])
    assert a.shape[0] == 2 and b.shape[0] == 2 and e.shape[0] == 2
    assert np.isfinite(a).all()
