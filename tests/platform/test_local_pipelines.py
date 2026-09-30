"""The Kubeflow Pipelines local runner behind PipelineRunner, on a trivial compiled pipeline.

No `from __future__ import annotations` here: kfp reads the component annotations at
definition time and a string annotation reads as an artifact type."""

from pathlib import Path

import pytest

from nw.platform.base import PipelineRunner, RunStatus
from nw.platform.local import LocalPipelineRunner, load_pipeline

pytestmark = pytest.mark.session06


@pytest.fixture
def compiled(tmp_path: Path) -> Path:
    kfp = pytest.importorskip("kfp")
    from kfp import compiler, dsl

    @dsl.component(base_image="python:3.12-slim")
    def add(a: int, b: int) -> int:
        return a + b

    @dsl.pipeline(name="probe")
    def probe(a: int = 1, b: int = 2) -> int:
        return add(a=a, b=b).output

    out = tmp_path / "probe.yaml"
    compiler.Compiler().compile(probe, str(out))
    assert kfp.__version__.startswith("2.")
    return out


def test_load_pipeline_accepts_yaml_callable_and_dotted():
    pytest.importorskip("kfp")

    def my_pipeline() -> None:
        pass

    name, fn = load_pipeline(my_pipeline)
    assert name == "my_pipeline" and fn is my_pipeline
    name, fn = load_pipeline("json:dumps")
    assert name == "dumps" and fn({"a": 1}) == '{"a": 1}'
    with pytest.raises(ValueError):
        load_pipeline("no-colon-and-no-suffix")
    with pytest.raises(FileNotFoundError):
        load_pipeline("missing.yaml")


def test_load_pipeline_resolves_a_name_and_compiles_when_missing(
    compiled: Path, tmp_path: Path, monkeypatch
):
    """`make pipeline-submit PIPELINE=triage` on the Local track: the bare name means
    `<NW_PIPELINE_DIR>/triage.yaml`, compiled on first use."""
    from nw.pipelines.kfp import pipelines as kfp_pipelines

    out = tmp_path / "compiled"
    monkeypatch.setenv("NW_PIPELINE_DIR", str(out))
    calls: list[Path] = []

    def fake_compile_all(out_dir: Path, image: str) -> dict:
        calls.append(Path(out_dir))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        for name in ("triage", "semantic", "retrain-triage", "retrain-semantic"):
            (Path(out_dir) / f"{name}.yaml").write_text(compiled.read_text())
        return {}

    monkeypatch.setattr(kfp_pipelines, "compile_all", fake_compile_all)
    name, component = load_pipeline("triage")
    assert name == "probe" and callable(component) and calls == [out]
    name, _ = load_pipeline("retrain-semantic")
    assert name == "probe" and calls == [out], "compiled once, then read from disk"
    with pytest.raises(ValueError):
        load_pipeline("policy")


def test_template_path_overrides_the_name(compiled: Path, tmp_path: Path, tenant):
    runner = LocalPipelineRunner(tmp_path / "runs", "subprocess")
    run = runner.submit(tenant, "triage", {"template_path": str(compiled), "a": 1, "b": 1})
    assert run.pipeline == "probe"
    done = runner.wait(tenant, run, timeout_s=300)
    assert done.status is RunStatus.SUCCEEDED, "\n".join(runner.logs(tenant, run))
    assert done.outputs.get("Output") == "2"


def test_submit_wait_status_and_logs(compiled: Path, tmp_path: Path, tenant):
    runner = LocalPipelineRunner(tmp_path / "runs", "subprocess")
    assert isinstance(runner, PipelineRunner)
    run = runner.submit(tenant, str(compiled), {"a": 3, "b": 4})
    assert run.pipeline == "probe" and run.status is RunStatus.RUNNING
    done = runner.wait(tenant, run, timeout_s=300)
    assert done.status is RunStatus.SUCCEEDED, "\n".join(runner.logs(tenant, run))
    assert done.outputs.get("Output") == "7"
    assert runner.status(tenant, run).status is RunStatus.SUCCEEDED
    assert any("SUCCESS" in line for line in runner.logs(tenant, run))
    assert (Path(done.url) / "run.json").exists()


def test_failure_is_recorded_not_raised(tmp_path: Path, tenant):
    pytest.importorskip("kfp")

    def boom(**_: object) -> None:
        raise RuntimeError("step exploded")

    runner = LocalPipelineRunner(tmp_path / "runs")
    run = runner.submit(tenant, boom, {})
    done = runner.wait(tenant, run, timeout_s=60)
    assert done.status is RunStatus.FAILED
    assert any("step exploded" in line for line in runner.logs(tenant, run))
    with pytest.raises(KeyError):
        runner.status(tenant, run.__class__("x", "nope", RunStatus.QUEUED))


def test_a_run_is_a_child_process_and_never_swaps_the_callers_streams(
    compiled: Path, tmp_path: Path, tenant
):
    """The runner used to redirect `sys.stdout` and replace `sys.executable` from a thread,
    which changed them for the whole process while a run was going (audit 01 Medium)."""
    import sys

    stdout, executable = sys.stdout, sys.executable
    runner = LocalPipelineRunner(tmp_path / "runs", "subprocess")
    run = runner.submit(tenant, str(compiled), {"a": 2, "b": 2})
    child = runner._children[run.run_id]
    assert sys.stdout is stdout and sys.executable == executable
    done = runner.wait(tenant, run, timeout_s=300)
    assert done.status is RunStatus.SUCCEEDED and done.outputs.get("Output") == "4"
    assert child.poll() == 0 and sys.stdout is stdout and sys.executable == executable
    assert not LocalPipelineRunner.runs_in_process, "the run outlives a CLI that submits"


def test_a_child_that_dies_is_recorded_as_failed(compiled: Path, tmp_path: Path, tenant):
    runner = LocalPipelineRunner(tmp_path / "runs", "subprocess")
    run = runner.submit(tenant, str(compiled), {"a": 1, "b": 1})
    runner._children[run.run_id].kill()
    runner._children[run.run_id].wait()
    run_dir = runner.run_dir(tenant, run.run_id)
    import json

    record = json.loads((run_dir / "run.json").read_text())
    if record["status"] not in ("succeeded", "failed"):
        assert runner.status(tenant, run).status is RunStatus.FAILED
        assert "exited" in json.loads((run_dir / "run.json").read_text())["error"]
