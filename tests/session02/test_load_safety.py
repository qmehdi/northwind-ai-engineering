"""A pickled model is code: it is loaded only when its bytes match a hash recorded somewhere
else, the registry entry when the caller has it, else the training run's metadata."""

import json
import shutil

import pytest

from nw.triage.model import TriageModel, sha256_of

pytestmark = pytest.mark.session02


def test_no_hash_means_no_load(trained, tmp_path):
    model, _, out = trained
    path = tmp_path / "artifact"  # a copy: the trained artifact is shared by the session
    shutil.copytree(out / model.version, path)
    meta = json.loads((path / "metadata.json").read_text())
    meta.pop("model_sha256")
    (path / "metadata.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="refusing to unpickle"):
        TriageModel.load(path)
    assert TriageModel.load(path, expected_sha256=sha256_of(path / "model.joblib")).version


def test_the_registry_hash_wins_over_the_metadata(trained):
    model, _, out = trained
    path = out / model.version
    with pytest.raises(ValueError, match="checksum"):
        TriageModel.load(path, expected_sha256="0" * 64)
