"""Ship the learner's code with every cloud pipeline run.

The pipeline image (`nw-pipelines`) carries the dependencies and a copy of `nw/` from the
day it was built. A learner changes `nw/` all course long, so a run that used the image's copy
would retrain yesterday's model: the gate would fail on the naive model and nothing would reach
the registry. Every cloud submit therefore packs the submitting checkout's `nw/` into a source
bundle and every step runs through the launcher below, which puts that bundle in front of the
image's copy. The image changes only when the dependencies change (`uv.lock`); the code travels
with the run.

The flow, per track (the same bundle and the same launcher everywhere):

1. `build_bundle(project)` writes `nw-source-<sha>.tar.gz`: every file under `nw/` (no caches),
   plus `nw_source.json` with the bundle's SHA-256, the commit (`nw.platform.lineage`), the
   `uv.lock` hash and the time. The archive is byte-for-byte reproducible, so the same code
   gives the same sha and the upload is skipped when it is already there.
2. The platform client uploads it and passes its URI as the pipeline parameter `source_uri`:
   AWS to `s3://<artifacts>/tenants/<tenant>/source/` (a ProcessingInput of every step, and
   the default of the pipeline definition, so the weekly schedule runs the last submitted
   code); Google Cloud to `gs://<artifacts>/<prefix>/source/` (the components read it through
   the `/gcs/` mount; `latest.tar.gz` beside it is what the Cloud Scheduler job passes);
   Azure as a job input uploaded with the submission.
3. Every step runs `python -m nw.pipelines.source run --source <uri> -- <module> <args>`. The
   launcher extracts the bundle, then starts the step in a fresh interpreter with the bundle
   first on `sys.path`, exports `NW_GIT_SHA`, `NW_SOURCE_SHA256_12` and `NW_UV_LOCK_SHA256_12`
   from the manifest for the lineage tags, and warns when the bundle's `uv.lock` differs from
   the image's (`NW_IMAGE_LOCK_SHA256_12`): code changes need no rebuild, dependency changes do
   (`make images-<track> IMAGES=pipelines`).

With no `--source` (the Local track, whose steps already run from the checkout) the launcher runs
the step as `python -m` would.

    uv run python -m nw.pipelines.source build --out artifacts/source   # the bundle, and its sha
    uv run python -m nw.pipelines.source run --source <uri> -- nw.pipelines.steps.register ...
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MANIFEST = "nw_source.json"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
# Runs the step module in a fresh interpreter with the bundle ahead of everything else. The
# interpreter's own first entry (the working directory, which is /app in the image and holds
# the image's copy of nw/) comes after it.
_INNER = (
    "import runpy, sys; src = sys.argv[1]; mod = sys.argv[2]; sys.path.insert(0, src) if src "
    "else None; sys.argv = [mod] + sys.argv[3:]; runpy.run_module(mod, run_name='__main__', "
    "alter_sys=True)"
)


@dataclass(frozen=True)
class Bundle:
    path: Path
    sha256_12: str
    git_sha: str
    files: int

    @property
    def name(self) -> str:
        return self.path.name


def _files(root: Path) -> list[Path]:
    out = []
    for p in sorted((root / "nw").rglob("*")):
        if not p.is_file() or p.suffix in SKIP_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        out.append(p)
    return out


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def _archive(entries: list[tuple[str, bytes]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in entries:
            _add(tar, name, data)
    # gzip without a timestamp or a file name, so equal code gives equal bytes
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, filename="") as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def build_bundle(project: Path = Path("."), out: Path | None = None) -> Bundle:
    """`<out>/nw-source-<sha>.tar.gz` from `<project>/nw`. The sha covers the code only, so a
    rebuild of unchanged code has the same name; the manifest adds the commit and lock hash."""
    from nw.platform.lineage import git_sha, lock_sha

    project = Path(project).resolve()
    if not (project / "nw" / "__init__.py").is_file():
        raise FileNotFoundError(f"{project} has no nw/ package to ship")
    entries = [(p.relative_to(project).as_posix(), p.read_bytes()) for p in _files(project)]
    digest = hashlib.sha256()
    for name, data in entries:
        digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    sha = digest.hexdigest()[:12]
    manifest = {
        "sha256_12": sha,
        "git_sha": git_sha(cwd=project),
        "uv_lock_sha256_12": lock_sha(project / "uv.lock"),
        "files": len(entries),
    }
    entries.append((MANIFEST, json.dumps(manifest, indent=1, sort_keys=True).encode()))
    target_dir = Path(out) if out is not None else Path(tempfile.mkdtemp(prefix="nw-bundle-"))
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"nw-source-{sha}.tar.gz"
    path.write_bytes(_archive(entries))
    return Bundle(path=path, sha256_12=sha, git_sha=manifest["git_sha"], files=len(entries) - 1)


def checkout() -> Path:
    """The checkout whose `nw/` is running: the directory holding the imported package."""
    import nw

    return Path(nw.__file__).resolve().parents[1]


def default_bundle() -> Bundle:
    """The bundle a platform client ships on submit: this checkout, into the temp directory."""
    return build_bundle(checkout(), Path(tempfile.gettempdir()) / "nw-bundles")


def local_path(uri: str) -> str:
    """Where a bundle URI is inside a step: `gs://b/k` is `/gcs/b/k` on Vertex; a directory
    (a SageMaker ProcessingInput) means the one bundle inside it."""
    path = "/gcs/" + uri[len("gs://") :] if uri.startswith("gs://") else uri
    if os.path.isdir(path):
        found = sorted(f for f in os.listdir(path) if f.endswith(".tar.gz"))
        if len(found) != 1:
            raise SystemExit(f"{path}: expected one source bundle, found {len(found)}")
        path = os.path.join(path, found[0])
    return path


def extract(uri: str, into: Path | None = None) -> tuple[Path, dict]:
    """The bundle unpacked into a fresh directory, and its manifest. A bundle that is named
    but missing is an error: running the image's copy instead is exactly the failure this
    module exists to prevent."""
    path = local_path(uri)
    if not os.path.isfile(path):
        raise SystemExit(
            f"source bundle {uri} not found at {path}: refusing to run the image's code"
        )
    target = Path(into) if into else Path(tempfile.mkdtemp(prefix="nw-source-"))
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "r:gz") as tar:
        tar.extractall(target, filter="data")
    manifest_path = target / MANIFEST
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if not (target / "nw" / "__init__.py").is_file():
        raise SystemExit(f"source bundle {uri} holds no nw/ package")
    return target, manifest


def launch_env(manifest: dict, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a step runs with: the bundle's lineage, unless the caller set it."""
    env = dict(os.environ if base is None else base)
    for var, key in (
        ("NW_GIT_SHA", "git_sha"),
        ("NW_SOURCE_SHA256_12", "sha256_12"),
        ("NW_UV_LOCK_SHA256_12", "uv_lock_sha256_12"),
    ):
        value = str(manifest.get(key) or "")
        if value and value != "nogit" and not env.get(var):
            env[var] = value
    return env


def lock_drift(manifest: dict, env: dict[str, str]) -> str | None:
    image = env.get("NW_IMAGE_LOCK_SHA256_12", "")
    bundle = str(manifest.get("uv_lock_sha256_12") or "")
    if image and bundle and image != bundle:
        return (
            f"uv.lock differs: bundle {bundle}, image {image}. The code is yours; the "
            "dependencies are the image's. Rebuild the pipelines image when pyproject changes."
        )
    return None


def platform_settings(text: str | None) -> dict[str, str]:
    """`--env '{"NW_TRACK": "gcp", ...}'`: only `NW_*` names, all values strings."""
    if not text:
        return {}
    try:
        values = json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"--env is not a JSON object: {exc}") from exc
    if not isinstance(values, dict):
        raise SystemExit("--env must be a JSON object of NW_* settings")
    bad = sorted(k for k in values if not str(k).startswith("NW_"))
    if bad:
        raise SystemExit(f"--env takes NW_* settings only, not {', '.join(bad)}")
    return {str(k): str(v) for k, v in values.items()}


def run(
    source: str | None, module: str, args: list[str], settings: dict[str, str] | None = None
) -> int:
    src = ""
    env = dict(os.environ)
    for key, value in (settings or {}).items():
        env.setdefault(key, value)
    if source:
        target, manifest = extract(source)
        src = str(target)
        env = launch_env(manifest, env)
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        print(
            f"nw source {manifest.get('sha256_12', '?')} (commit {manifest.get('git_sha', '?')}) "
            f"from {source}",
            file=sys.stderr,
        )
        if not env.get("NW_IMAGE_LOCK_SHA256_12"):
            from nw.platform.lineage import lock_sha

            # the image's own lock, when the build copied it beside the code (/app/uv.lock)
            env["NW_IMAGE_LOCK_SHA256_12"] = lock_sha(Path("uv.lock"), {})
        warning = lock_drift(manifest, env)
        if warning:
            print(f"warning: {warning}", file=sys.stderr)
    started = time.monotonic()
    code = subprocess.run([sys.executable, "-c", _INNER, src, module, *args], env=env).returncode
    print(f"{module} exited {code} after {time.monotonic() - started:.1f}s", file=sys.stderr)
    return code


def launcher(module: str, source: str | None = None) -> list[str]:
    """The argv a step's container runs: `python -m nw.pipelines.source run [--source X] --
    <module>`; the step's own arguments follow."""
    argv = ["python", "-m", "nw.pipelines.source", "run"]
    if source:
        argv += ["--source", source]
    return [*argv, "--", module]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nw.pipelines.source", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="write the source bundle of this checkout")
    b.add_argument("--project", type=Path, default=Path("."))
    b.add_argument("--out", type=Path, default=Path("artifacts/source"))
    r = sub.add_parser("run", help="run a step module on the bundle's code")
    r.add_argument("--source", default="", help="bundle path or URI; empty runs the local code")
    r.add_argument("--env", default="", help="JSON object of NW_* settings for the step")
    r.add_argument("module")
    r.add_argument("args", nargs=argparse.REMAINDER)
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["run"] and "--" in raw:
        cut = raw.index("--")
        head, rest = raw[:cut], raw[cut + 1 :]
        if not rest:
            ap.error("run needs a module after --")
        args = ap.parse_args([*head, rest[0]])
        return run(args.source or None, args.module, rest[1:], platform_settings(args.env))
    args = ap.parse_args(raw)
    if args.command == "build":
        bundle = build_bundle(args.project, args.out)
        print(f"{bundle.path} sha {bundle.sha256_12} commit {bundle.git_sha}, {bundle.files} files")
        return 0
    return run(args.source or None, args.module, list(args.args), platform_settings(args.env))


if __name__ == "__main__":
    sys.exit(main())
