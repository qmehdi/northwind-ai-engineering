"""`model.tar.gz` for the SageMaker Model Registry: the artifact plus `code/`.

    uv run python -m nw.serving.sagemaker.package artifacts/triage/latest --into dist/triage
    uv run python -m nw.serving.sagemaker.package artifacts/semantic/latest \
        --into dist/semantic --write-code

The prebuilt containers unpack the tarball into `/opt/ml/model` and run `code/inference.py`
(the toolkit's default entry point; `SAGEMAKER_PROGRAM=inference.py` and
`SAGEMAKER_SUBMIT_DIRECTORY=/opt/ml/model/code` name the same thing explicitly). `code/` holds:

- `inference.py`: a copy of `nw.serving.sagemaker.inference`
- `requirements.txt`: what the container lacks (pydantic for Project 1; onnxruntime and
  transformers for Project 2)
- for Project 1, `nw/triage/{features,model}.py` and the two `__init__.py`: the joblib pickle
  references the feature transformers by module path, and the container has no `nw` package

Checkpoints and the fp32 ONNX graph stay out, as in `nw.pipelines.steps.package`. `--write-code`
leaves `code/` inside the artifact directory too, so a later `nw.pipelines.steps.package` of the
same version (the training step's `--package-dir`) produces the same valid layout.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

import nw
import nw.triage.features
import nw.triage.model
from nw.serving.sagemaker import inference as _inference

CODE_DIR = "code"
NOT_PACKAGED = ("checkpoint.pt", "best.pt", "model.onnx")
REQUIREMENTS = {
    "triage": ["pydantic>=2.7", "joblib>=1.4"],
    "semantic": ["onnxruntime>=1.19", "transformers>=4.44", "numpy>=1.26"],
}
VENDORED = {
    "nw/__init__.py": Path(nw.__file__),
    "nw/triage/__init__.py": Path(nw.triage.model.__file__).with_name("__init__.py"),
    "nw/triage/features.py": Path(nw.triage.features.__file__),
    "nw/triage/model.py": Path(nw.triage.model.__file__),
}


def kind_of(version_dir: Path) -> str:
    d = Path(version_dir)
    if (d / "model.joblib").is_file():
        return "triage"
    if (d / "model.int8.onnx").is_file() or (d / "model.onnx").is_file():
        return "semantic"
    raise FileNotFoundError(f"{d}: not a triage or semantic artifact")


def write_code(version_dir: Path, into: Path | None = None) -> Path:
    """Write `code/` for the artifact, into the artifact itself by default. Returns the dir."""
    version_dir = Path(version_dir)
    kind = kind_of(version_dir)
    code = Path(into) if into is not None else version_dir / CODE_DIR
    code.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(_inference.__file__), code / "inference.py")
    (code / "requirements.txt").write_text("\n".join(REQUIREMENTS[kind]) + "\n", encoding="utf-8")
    if kind == "triage":
        for rel, src in VENDORED.items():
            dest = code / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    return code


def package(version_dir: Path, into: Path, name: str = "model.tar.gz") -> Path:
    """The tarball: every artifact file except the ones that never ship, plus `code/`
    generated on the fly (the artifact directory is left as it is)."""
    version_dir = Path(version_dir)
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    target = into / name
    with tempfile.TemporaryDirectory(prefix="nw-code-") as tmp:
        code = write_code(version_dir, Path(tmp) / CODE_DIR)
        with tarfile.open(target, "w:gz") as tar:
            for path in sorted(version_dir.rglob("*")):
                rel = path.relative_to(version_dir)
                if not path.is_file() or path.name in NOT_PACKAGED or rel.parts[0] == CODE_DIR:
                    continue
                tar.add(path, arcname=str(rel))
            for path in sorted(code.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=str(Path(CODE_DIR) / path.relative_to(code)))
    return target


def layout(tarball: Path) -> list[str]:
    with tarfile.open(tarball, "r:gz") as tar:
        return sorted(m.name for m in tar.getmembers() if m.isfile())


def is_valid(tarball: Path) -> tuple[bool, list[str]]:
    """Whether the tarball is a model artifact the prebuilt containers can serve, and why not."""
    names = set(layout(tarball))
    problems = []
    if "metadata.json" not in names:
        problems.append("metadata.json missing at the archive root")
    if f"{CODE_DIR}/inference.py" not in names:
        problems.append(f"{CODE_DIR}/inference.py missing")
    if f"{CODE_DIR}/requirements.txt" not in names:
        problems.append(f"{CODE_DIR}/requirements.txt missing")
    if "model.joblib" in names and f"{CODE_DIR}/nw/triage/model.py" not in names:
        problems.append("triage artifact without the vendored nw.triage modules")
    if "model.joblib" not in names and not ({"model.int8.onnx", "model.onnx"} & names):
        problems.append("no model file (model.joblib or model.int8.onnx)")
    return not problems, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("version_dir", type=Path, help="artifacts/<project>/<version> or latest")
    ap.add_argument("--into", type=Path, default=Path("dist"), help="where model.tar.gz goes")
    ap.add_argument("--write-code", action="store_true", help="also leave code/ in the artifact")
    args = ap.parse_args(argv)
    if args.write_code:
        write_code(args.version_dir)
    target = package(args.version_dir, args.into)
    ok, problems = is_valid(target)
    print(target)
    for line in layout(target):
        print(" ", line)
    if not ok:
        print("INVALID: " + "; ".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
