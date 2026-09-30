"""Project 2 steps on the tiny encoder: prep, train, export, benchmark, gate, register, in
the order the pipeline runs them, each reading what the one before wrote."""

from __future__ import annotations

import json

import pytest
import torch

from nw.pipelines.steps import (
    read_result,
    semantic_benchmark,
    semantic_data_prep,
    semantic_export,
    semantic_gate,
    semantic_train,
)
from nw.pipelines.steps import register as register_step
from nw.platform.base import Stage
from nw.semantic.promote import GatePolicy

pytestmark = pytest.mark.pipelines

PERMISSIVE = GatePolicy(
    min_p0_recall=0.0,
    min_tag_micro_f1=0.0,
    max_parity_fp32=1.0,
    max_int8_macro_f1_drop=1.0,
    max_int8_tag_f1_drop=1.0,
    max_int8_p95_ms=1e9,
)


@pytest.fixture(scope="module")
def pipeline_run(tmp_path_factory, s3_file, spec, config, tiny_tokenizer):
    out = tmp_path_factory.mktemp("semantic")
    prep = semantic_data_prep.run(s3_file, out)
    train = semantic_train.run(
        s3_file,
        out,
        epochs=2,
        batch_size=16,
        accumulate=1,
        lr=1e-2,
        subset=0,
        spec=spec,
        device=torch.device("cpu"),
        config=config,
        tokenizer=tiny_tokenizer,
        log_every=1000,
    )
    version = train["version"]
    export = semantic_export.run(
        out, version, s3_file, n=5, package_dir=out, config=config, tokenizer=tiny_tokenizer
    )
    bench = semantic_benchmark.run(
        out, version, s3_file, latency_n=5, config=config, tokenizer=tiny_tokenizer
    )
    return out, prep, train, export, bench


def test_data_prep_profiles_tags_and_the_contract(pipeline_run):
    out, prep, *_ = pipeline_run
    assert prep["ok"] and prep["n"] == 600 and prep["tags_in_training"] >= 8
    profile = json.loads((out / "data_profile.json").read_text())
    assert sum(profile["tag_share"].values()) > 0 and "text_length_bins" in profile


def test_data_prep_flags_untagged_rows_and_unknown_tags(tmp_path, s3_rows):
    rows = [dict(r, tags=[]) for r in s3_rows]
    path = tmp_path / "untagged.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert semantic_data_prep.main(["--data", str(path), "--out", str(tmp_path / "a")]) == 1
    checks = {f["check"] for f in read_result(tmp_path / "a", "semantic_data_prep")["findings"]}
    assert "tags" in checks
    rows = [dict(r, tags=r["tags"] + ["Teleport"]) for r in s3_rows]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert semantic_data_prep.main(["--data", str(path), "--out", str(tmp_path / "b")]) == 0
    findings = read_result(tmp_path / "b", "semantic_data_prep")["findings"]
    assert any(f["check"] == "tag_vocabulary" and not f["blocking"] for f in findings)


def test_train_step_is_the_course_training_run(pipeline_run):
    out, _, train, *_ = pipeline_run
    meta = json.loads((out / train["version"] / "metadata.json").read_text())
    assert (
        meta["epochs"] == 2
        and meta["metrics"]["test"]["tag_micro_f1"] == train["metrics"]["tag_micro_f1"]
    )
    assert (out / train["version"] / "MODEL_CARD.md").exists() and not (out / "latest").exists()
    runs = [json.loads(line) for line in (out / "runs.jsonl").read_text().splitlines()]
    assert runs[-1]["version"] == train["version"]


def test_export_and_benchmark_feed_the_gate(pipeline_run):
    out, _, train, export, bench = pipeline_run
    version = train["version"]
    assert export["parity_fp32_ok"] and (out / version / "model.int8.onnx").exists()
    assert (out / version / "tokenizer").is_dir() and (out / "model.tar.gz").exists()
    import tarfile

    with tarfile.open(out / "model.tar.gz") as tar:
        names = tar.getnames()
    assert "model.int8.onnx" in names and "model.onnx" not in names and "best.pt" not in names
    assert bench["rows"][1:] == [
        "Project 2: PyTorch fp32",
        "Project 2: ONNX fp32",
        "Project 2: ONNX int8",
    ]
    assert (out / version / "benchmark_triage").is_dir(), "a Project 1 reference was trained"
    assert (out / version / "benchmark.json").exists()


def test_gate_decides_on_the_candidate_and_register_follows(
    pipeline_run, tmp_path, registry, tenant
):
    out, _, train, *_ = pipeline_run
    version = train["version"]
    strict = semantic_gate.run(out, version, production_summary="none")
    assert strict["passed_int"] == int(strict["passed"]) and "int8_p95_ms" in strict["metrics"]
    decision = semantic_gate.run(out, version, production_summary="none", policy=PERMISSIVE)
    assert decision["passed"] and decision["production"] is None
    assert json.loads((out / version / "gate.json").read_text())["candidate"] == version
    assert not (out / "latest").exists(), "the pipeline gate never moves latest"
    # The served graph travels with the artifact, as with nw.semantic.promote.
    serving = json.loads((out / version / "serving.json").read_text())
    assert serving["format"] == decision["served_format"]
    assert serving["quantized"] == (decision["served_format"] == "int8")
    result = register_step.run(out, None, pipeline="semantic", registry=registry, tenant=tenant)
    assert result["version"] == version
    versions = registry.versions(tenant, "semantic")
    assert len(versions) == 1 and versions[0].name == "northwind-alice-semantic"
    assert versions[0].stage == Stage.CANDIDATE and versions[0].tags["pipeline"] == "semantic"
    assert versions[0].tags["served_format"] == decision["served_format"]
    assert versions[0].metrics["tag_micro_f1"] == train["metrics"]["tag_micro_f1"]


def test_gate_names_a_missing_export(tmp_path, s3_file, spec, config, tiny_tokenizer):
    out = tmp_path / "semantic"
    train = semantic_train.run(
        s3_file,
        out,
        epochs=1,
        accumulate=1,
        subset=0,
        spec=spec,
        device=torch.device("cpu"),
        config=config,
        tokenizer=tiny_tokenizer,
        log_every=1000,
    )
    with pytest.raises(SystemExit, match="export_report.json is missing"):
        semantic_gate.run(out, train["version"], production_summary="none")
