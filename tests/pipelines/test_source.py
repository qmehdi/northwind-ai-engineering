"""The source bundle and the launcher: a cloud pipeline step runs the learner's `nw/`, not the
copy baked into the pipelines image (audit 04 C1)."""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from nw.pipelines import source

ROOT = Path(__file__).resolve().parents[2]

MARKER_STEP = """
import json, os, sys
from pathlib import Path
out = Path(sys.argv[sys.argv.index("--out") + 1])
out.mkdir(parents=True, exist_ok=True)
(out / "ran.json").write_text(json.dumps({
    "code": "learner",
    "file": __file__,
    "git_sha": os.environ.get("NW_GIT_SHA", ""),
    "source_sha": os.environ.get("NW_SOURCE_SHA256_12", ""),
    "track": os.environ.get("NW_TRACK", ""),
}))
"""


def _project(tmp_path: Path) -> Path:
    """A checkout whose triage data check is not the one installed: it writes a marker."""
    project = tmp_path / "checkout"
    steps = project / "nw" / "pipelines" / "steps"
    steps.mkdir(parents=True)
    for pkg in (project / "nw", project / "nw" / "pipelines", steps):
        (pkg / "__init__.py").write_text("")
    (steps / "triage_data_check.py").write_text(MARKER_STEP)
    (steps / "__pycache__").mkdir()
    (steps / "__pycache__" / "junk.cpython-312.pyc").write_bytes(b"\0")
    (project / "uv.lock").write_text("lock v1\n")
    return project


def test_bundle_is_reproducible_and_leaves_caches_out(tmp_path):
    project = _project(tmp_path)
    one = source.build_bundle(project, tmp_path / "a")
    two = source.build_bundle(project, tmp_path / "b")
    assert one.sha256_12 == two.sha256_12 and one.path.read_bytes() == two.path.read_bytes()
    assert one.name == f"nw-source-{one.sha256_12}.tar.gz"
    with tarfile.open(one.path) as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile(source.MANIFEST).read())
    assert "nw/pipelines/steps/triage_data_check.py" in names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert manifest["sha256_12"] == one.sha256_12 and manifest["files"] == one.files
    assert len(manifest["uv_lock_sha256_12"]) == 12
    (project / "nw" / "pipelines" / "steps" / "triage_data_check.py").write_text("# changed\n")
    assert source.build_bundle(project, tmp_path / "c").sha256_12 != one.sha256_12


def test_the_launcher_runs_the_bundle_not_the_installed_copy(tmp_path):
    """Run from the solution directory, where the real `nw/` sits first on the path the way
    `/app/nw` does in the image: the bundle's step still wins."""
    bundle = source.build_bundle(_project(tmp_path), tmp_path / "bundle")
    out = tmp_path / "out"
    code = subprocess.run(
        [sys.executable, "-m", "nw.pipelines.source", "run", "--source", str(bundle.path)]
        + ["--env", json.dumps({"NW_TRACK": "gcp"})]
        + ["--", "nw.pipelines.steps.triage_data_check", "--out", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={k: v for k, v in __import__("os").environ.items() if k not in ("NW_TRACK",)},
    )
    assert code.returncode == 0, code.stderr
    ran = json.loads((out / "ran.json").read_text())
    assert ran["code"] == "learner" and "nw-source-" in ran["file"]
    assert ran["source_sha"] == bundle.sha256_12
    assert ran["track"] == "gcp", "--env hands the step its platform"
    assert f"nw source {bundle.sha256_12}" in code.stderr


def test_a_directory_input_means_the_one_bundle_inside(tmp_path):
    """SageMaker downloads the S3 object into a ProcessingInput directory."""
    bundle = source.build_bundle(_project(tmp_path), tmp_path / "input")
    assert source.local_path(str(tmp_path / "input")) == str(bundle.path)
    assert source.local_path("gs://b/p/nw-source-x.tar.gz") == "/gcs/b/p/nw-source-x.tar.gz"


def test_a_named_bundle_that_is_missing_refuses_to_run_the_image_code(tmp_path):
    with pytest.raises(SystemExit, match="refusing to run the image's code"):
        source.extract(str(tmp_path / "nw-source-gone.tar.gz"))


def test_env_takes_nw_settings_only_and_lock_drift_warns():
    assert source.platform_settings('{"NW_TRACK": "gcp"}') == {"NW_TRACK": "gcp"}
    with pytest.raises(SystemExit, match="NW_"):
        source.platform_settings('{"PATH": "/tmp"}')
    assert source.lock_drift({"uv_lock_sha256_12": "a"}, {"NW_IMAGE_LOCK_SHA256_12": "b"})
    assert source.lock_drift({"uv_lock_sha256_12": "a"}, {"NW_IMAGE_LOCK_SHA256_12": "a"}) is None
    env = source.launch_env({"git_sha": "abc", "sha256_12": "s1"}, {})
    assert env["NW_GIT_SHA"] == "abc" and env["NW_SOURCE_SHA256_12"] == "s1"
    assert "NW_GIT_SHA" not in source.launch_env({"git_sha": "nogit"}, {})


def test_launcher_argv_and_every_step_definition_use_it():
    assert source.launcher("nw.pipelines.steps.register", "/opt/in") == [
        "python", "-m", "nw.pipelines.source", "run", "--source", "/opt/in", "--",
        "nw.pipelines.steps.register",
    ]  # fmt: skip


def test_the_checkout_bundles(tmp_path):
    bundle = source.build_bundle(ROOT, tmp_path)
    with tarfile.open(bundle.path) as tar:
        names = set(tar.getnames())
    assert {"nw/pipelines/source.py", "nw/pipelines/steps/register.py"} <= names
    assert bundle.files > 50
