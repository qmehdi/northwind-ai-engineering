"""The SageMaker adapters: the inference handler round-trips both artifacts, the tarball has the
layout the prebuilt containers expect, the baseline files describe text_length."""

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from nw.pipelines.steps import package as pipeline_package
from nw.serving.sagemaker import (
    IMAGES,
    PYTORCH_IMAGE,
    SKLEARN_IMAGE,
    baseline,
    inference,
    package,
    preprocessor,
)

TICKETS = [
    {
        "subject": "URGENT production down",
        "body": "All users get 502 errors, production is blocked.",
    },
    {"subject": "How to export audit log", "body": "Is there documentation on exporting to CSV?"},
]


@pytest.fixture(scope="session")
def triage_dir(trained) -> Path:
    _, _, out = trained
    return out / "latest"


@pytest.fixture(scope="session")
def semantic_dir(exported_tiny) -> Path:
    out, _ = exported_tiny
    assert (out / "model.int8.onnx").exists() and (out / "tokenizer").is_dir()
    return out


def test_images_are_the_prebuilt_x86_containers():
    assert IMAGES == {"triage": SKLEARN_IMAGE, "semantic": PYTORCH_IMAGE}
    assert "sagemaker-scikit-learn:1.9-0-cpu-py3" in SKLEARN_IMAGE
    assert "pytorch-inference:2.6.0-cpu-py312" in PYTORCH_IMAGE


def test_triage_handlers_round_trip(triage_dir):
    model = inference.model_fn(str(triage_dir))
    assert model.kind == "triage"
    body = json.dumps({"instances": TICKETS}).encode()
    instances = inference.input_fn(body, "application/json")
    out = json.loads(
        inference.output_fn(inference.predict_fn(instances, model), "application/json")
    )
    preds = out["predictions"]
    assert len(preds) == 2 and preds[0]["priority"] == "P0" and preds[1]["priority"] != "P0"
    assert preds[0]["text_length"] == len(TICKETS[0]["subject"]) + len(TICKETS[0]["body"])
    assert preds[0]["model_version"] == model.version and set(preds[0]["probabilities"]) == {
        "P0",
        "P1",
        "P2",
        "P3",
    }
    lines = inference.output_fn(preds, "application/jsonlines").splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["priority"] == preds[1]["priority"]


def test_input_fn_accepts_every_shape_and_refuses_empty_bodies():
    one = inference.input_fn(json.dumps(TICKETS[0]))
    assert one == [TICKETS[0]]
    assert inference.input_fn(json.dumps(TICKETS)) == TICKETS
    lines = "\n".join(json.dumps(t) for t in TICKETS)
    assert inference.input_fn(lines, "application/jsonlines") == TICKETS
    with pytest.raises(ValueError):
        inference.input_fn(json.dumps({"subject": "x", "body": ""}))
    with pytest.raises(ValueError):
        inference.input_fn("a,b", "text/csv")


def test_semantic_handlers_round_trip(semantic_dir):
    model = inference.model_fn(str(semantic_dir))
    assert model.kind == "semantic" and model.file == "model.int8.onnx"
    preds = inference.predict_fn(inference.input_fn(json.dumps({"instances": TICKETS})), model)
    assert len(preds) == 2
    for p, t in zip(preds, TICKETS, strict=True):
        assert p["priority"] in {"P0", "P1", "P2", "P3"} and isinstance(p["tags"], list)
        assert abs(sum(p["priority_scores"].values()) - 1) < 0.01
        assert p["text_length"] == len(t["subject"]) + len(t["body"])
        assert p["format"] == "model.int8.onnx" and p["model_version"] == model.version


def test_model_fn_refuses_an_unknown_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        inference.model_fn(str(tmp_path))


def test_triage_tarball_layout(triage_dir, tmp_path):
    target = package.package(triage_dir, tmp_path / "dist")
    names = package.layout(target)
    assert "metadata.json" in names and "model.joblib" in names
    assert "code/inference.py" in names and "code/requirements.txt" in names
    assert {
        "code/nw/__init__.py",
        "code/nw/triage/__init__.py",
        "code/nw/triage/features.py",
        "code/nw/triage/model.py",
    } <= set(names)
    ok, problems = package.is_valid(target)
    assert ok, problems
    assert not (triage_dir / "code").exists()  # the artifact itself was left alone


def test_semantic_tarball_ships_int8_only_with_the_runtime_requirements(semantic_dir, tmp_path):
    target = package.package(semantic_dir, tmp_path / "dist")
    names = package.layout(target)
    assert "model.int8.onnx" in names and "model.onnx" not in names
    assert "tag_thresholds.npy" in names and any(n.startswith("tokenizer/") for n in names)
    assert "code/nw/triage/model.py" not in names
    import tarfile

    with tarfile.open(target) as tar:
        req = tar.extractfile("code/requirements.txt").read().decode()
    assert "onnxruntime" in req and "transformers" in req
    assert package.is_valid(target)[0]


def test_write_code_makes_the_pipeline_package_valid(triage_dir, tmp_path):
    """After `write_code`, the training step's own packager (nw.pipelines.steps.package)
    produces the layout the container needs: `--package-dir` output is a model artifact."""
    import shutil

    copy = tmp_path / "version"
    shutil.copytree(triage_dir, copy)
    package.write_code(copy)
    target = pipeline_package(copy, tmp_path / "dist")
    ok, problems = package.is_valid(target)
    assert ok, problems
    vendored = (copy / "code" / "nw" / "triage" / "model.py").read_bytes()
    assert vendored == Path(package.VENDORED["nw/triage/model.py"]).read_bytes()


def test_package_cli_prints_the_layout(triage_dir, tmp_path, capsys):
    assert package.main([str(triage_dir), "--into", str(tmp_path / "dist")]) == 0
    out = capsys.readouterr().out
    assert "code/inference.py" in out and "model.tar.gz" in out


@pytest.fixture
def profile_path(ticket_rows, tmp_path) -> Path:
    from nw.triage.data_check import profile

    p = tmp_path / "data_profile.json"
    p.write_text(json.dumps(asdict(profile(ticket_rows, "abc123abc123"))))
    return p


def test_baseline_files_describe_text_length(profile_path, ticket_file, tmp_path):
    stats_path, cons_path = baseline.write_baseline(profile_path, tmp_path / "baseline")
    stats = json.loads(stats_path.read_text())
    cons = json.loads(cons_path.read_text())
    feature = stats["features"][0]
    assert feature["name"] == "text_length" and feature["inferred_type"] == "Integral"
    n = stats["dataset"]["item_count"]
    buckets = feature["numerical_statistics"]["distribution"]["kll"]["buckets"]
    assert n > 0 and abs(sum(b["count"] for b in buckets) - n) <= len(buckets)
    assert all(b["lower_bound"] <= b["upper_bound"] for b in buckets)
    assert feature["numerical_statistics"]["common"] == {"num_present": n, "num_missing": 0}
    assert cons["features"][0]["name"] == "text_length"
    assert cons["monitoring_config"]["distribution_constraints"]["comparison_threshold"] == 0.1
    assert cons["data_sha256_12"] == "abc123abc123"
    # exact moments from the tickets file
    exact, _ = baseline.write_baseline(profile_path, tmp_path / "exact", ticket_file)
    ns = json.loads(exact.read_text())["features"][0]["numerical_statistics"]
    assert ns["min"] >= 0 and ns["max"] >= ns["mean"] >= ns["min"] and ns["std_dev"] > 0


def test_baseline_cli(profile_path, tmp_path, capsys):
    assert baseline.main(["--profile", str(profile_path), "--out", str(tmp_path / "b")]) == 0
    assert "statistics.json" in capsys.readouterr().out


def test_preprocessor_turns_a_captured_request_into_the_feature():
    record = SimpleNamespace(endpoint_input=SimpleNamespace(data=json.dumps(TICKETS[0])))
    assert preprocessor.preprocess_handler(record) == {
        "text_length": len(TICKETS[0]["subject"]) + len(TICKETS[0]["body"])
    }
    batch = {"endpoint_input": {"data": json.dumps({"instances": TICKETS})}}
    assert [d["text_length"] for d in preprocessor.preprocess_handler(batch)] == [
        len(t["subject"]) + len(t["body"]) for t in TICKETS
    ]
    assert preprocessor.preprocess_handler("not json") == {"text_length": len("not json")}


def test_base_tokenizer_downloads_are_pinned():
    from nw.config import HF_REVISIONS
    from nw.serving.sagemaker.inference import base_revision

    assert base_revision({"base": "x/y", "base_revision": "abc"}) == "abc"
    pinned = HF_REVISIONS["distilbert/distilbert-base-uncased"]
    assert base_revision({"base": "distilbert-base-uncased"}) == pinned
    with pytest.raises(ValueError):
        base_revision({"base": "someone/unpinned-model"})
