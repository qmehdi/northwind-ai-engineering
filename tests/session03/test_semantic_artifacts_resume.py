"""Resume finds an interrupted run: Ctrl-C leaves a checkpoint and no metadata.json."""

import os

import pytest

from nw.semantic.train import find_checkpoint

pytestmark = pytest.mark.session03


def test_resume_finds_an_interrupted_run_from_the_root_or_its_directory(tmp_path):
    finished = tmp_path / "20260930010000-nogit-a"
    stopped = tmp_path / "20260930020000-nogit-a"
    for d in (finished, stopped):
        d.mkdir()
        (d / "checkpoint.pt").write_bytes(b"x")
    (finished / "metadata.json").write_text("{}")
    os.utime(finished / "checkpoint.pt", (1, 1))  # the stopped run's checkpoint is newer
    assert find_checkpoint(stopped) == stopped / "checkpoint.pt"
    assert find_checkpoint(tmp_path) == stopped / "checkpoint.pt"
    assert find_checkpoint(stopped / "checkpoint.pt") == stopped / "checkpoint.pt"


def test_backtest_scores_a_never_gated_candidate_like_the_served_version(tmp_path):
    """The shadow scores a candidate in production's served graph; the backtest does the same."""
    import json

    from nw.semantic.backtest import as_served

    prod, cand = tmp_path / "prod", tmp_path / "cand"
    for d in (prod, cand):
        d.mkdir()
        (d / "metadata.json").write_text("{}")
    assert as_served(prod, cand) == (True, True)  # nothing gated: int8 both
    (prod / "serving.json").write_text(json.dumps({"format": "fp32", "quantized": False}))
    assert as_served(prod, cand) == (False, False)
    assert as_served(cand, prod) == (False, False)
