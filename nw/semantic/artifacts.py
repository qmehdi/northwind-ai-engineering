"""Where a Project 2 artifact lives, and which one a command means.

The artifact tree mirrors Project 1: `artifacts/semantic/<version>/` per training run,
`artifacts/semantic/latest` set only by the promotion gate, `runs.jsonl` and
`promotions.jsonl` beside them. Three shapes of path are accepted everywhere:

- a version directory or `latest`: it holds `metadata.json` directly
- a flat directory that holds `metadata.json` directly (the Colab zip unzips this way)
- the root `artifacts/semantic`: resolved to `latest` for serving, and to the newest
  candidate for export, benchmark and the gate, which prepare a candidate for promotion
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path("artifacts/semantic")


def is_version_dir(path: Path) -> bool:
    return (path / "metadata.json").is_file()


def versions(root: Path) -> list[str]:
    """Candidate directories under the root, oldest first; `latest` itself is excluded."""
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink() and is_version_dir(p)
    )


def newest_candidate(root: Path) -> Path:
    found = versions(root)
    if not found:
        raise FileNotFoundError(
            f"no trained candidate under {root}: run `make train-semantic` first"
        )
    return root / found[-1]


def resolve(path: Path | str, *, serve: bool = False) -> Path:
    """The version directory a path means.

    `serve=True` wants the promoted model: the root resolves to `latest` and nothing
    else, because the gate is the only thing that sets it. Otherwise the root resolves
    to the newest candidate, which is what export, benchmark and the gate work on.
    """
    path = Path(path)
    if is_version_dir(path):
        return path
    latest = path / "latest"
    if serve:
        if is_version_dir(latest):
            return latest
        raise FileNotFoundError(
            f"no served model at {path}: `latest` is set by the promotion gate "
            "(`make promote-semantic`), or point at a version directory"
        )
    return newest_candidate(path)
