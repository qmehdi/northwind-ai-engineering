"""A checkpoint from a shared bucket is read with `weights_only=True`: tensors and plain
containers load, a pickled object that would run code does not."""

import pickle

import pytest
import torch

from nw.semantic.train import load_checkpoint

pytestmark = pytest.mark.session03


class Payload:
    def __reduce__(self):
        return (print, ("this would run on load",))


def test_a_checkpoint_with_an_object_is_refused(tmp_path):
    path = tmp_path / "evil.pt"
    torch.save({"model": {}, "extra": Payload()}, path)
    model = torch.nn.Linear(2, 2)
    with pytest.raises(pickle.UnpicklingError):
        load_checkpoint(path, model)


def test_a_plain_checkpoint_loads(tmp_path):
    model = torch.nn.Linear(2, 2)
    path = tmp_path / "ok.pt"
    torch.save({"model": model.state_dict(), "epoch": 1, "step": 3, "best": 0.5}, path)
    assert load_checkpoint(path, torch.nn.Linear(2, 2))["step"] == 3
