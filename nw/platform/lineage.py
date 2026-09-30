"""Lineage: which code, which dependencies and which image produced an artifact.

A laptop run has a git checkout; a pipeline step in a container has none, which is how the
committed summaries ended up with `nogit`. Every source is tried in order, and the answer says
where it came from:

- git sha: `NW_GIT_SHA` (set by `nw.pipelines.source` from the bundle a cloud run ships), then
  `GITHUB_SHA` (GitHub Actions), then `git rev-parse` in the working directory, then the image
  label the build baked in (`NW_IMAGE_GIT_SHA`, from `org.opencontainers.image.revision`), else
  `nogit`.
- lock hash: the first twelve characters of the SHA-256 of `uv.lock` (`NW_UV_LOCK_SHA256_12`
  when the file is not at hand, which is what an image or a bundle sets).
- image digest: `NW_IMAGE_DIGEST` (the deploy passes the digest it pinned), else empty.

    uv run python -m nw.platform.lineage     # what this checkout would record
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

NOGIT = "nogit"
LOCK = Path("uv.lock")


def git_sha(env: Mapping[str, str] | None = None, cwd: Path | None = None) -> str:
    """The short commit of the code that is running (see the module docstring for the order)."""
    return git_sha_and_source(env, cwd)[0]


def git_sha_and_source(
    env: Mapping[str, str] | None = None, cwd: Path | None = None
) -> tuple[str, str]:
    e = os.environ if env is None else env
    for var in ("NW_GIT_SHA", "GITHUB_SHA"):
        value = (e.get(var) or "").strip()
        if value:
            return value[:12], var
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=10,
        ).stdout.strip()
        if out:
            return out, "git"
    except Exception:  # noqa: BLE001  no git, no checkout: try the image label
        pass
    label = (e.get("NW_IMAGE_GIT_SHA") or "").strip()
    if label:
        return label[:12], "image-label"
    return NOGIT, "none"


def lock_sha(path: Path = LOCK, env: Mapping[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    if Path(path).is_file():
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    return (e.get("NW_UV_LOCK_SHA256_12") or "").strip()


def image_digest(env: Mapping[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    return (e.get("NW_IMAGE_DIGEST") or "").strip()


def lineage(env: Mapping[str, str] | None = None, lock: Path = LOCK) -> dict[str, str]:
    """Every lineage field as strings, ready to be registry tags or run parameters."""
    sha, source = git_sha_and_source(env)
    out = {
        "git_sha": sha,
        "git_sha_source": source,
        "uv_lock_sha256_12": lock_sha(lock, env),
        "image_digest": image_digest(env),
    }
    e = os.environ if env is None else env
    bundle = (e.get("NW_SOURCE_SHA256_12") or "").strip()
    if bundle:
        out["source_sha256_12"] = bundle
    return out


def resolve(recorded: str | None, env: Mapping[str, str] | None = None) -> str:
    """`recorded` when it names a commit, else what the environment knows now: a step reading a
    `metadata.json` written as `nogit` in a container still records the bundle's commit."""
    if recorded and recorded != NOGIT:
        return recorded
    return git_sha(env)


def main() -> int:
    print(json.dumps(lineage(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
