"""Versioned data: a manifest of every file the models, the index and the evaluations read.

    uv run python -m nw.data_manifest                  # write data/MANIFEST.json
    uv run python -m nw.data_manifest --check          # exit 1 when a file changed without it
    uv run python -m nw.data_manifest --dataset-version 2026.10.0

A model card says which data trained the model; a baseline says which golden set produced
it. Both point at files, and files change. The manifest pins each one by content: sha256,
size and row count for the tickets, the accounts, every policy document, the golden sets,
the adversarial set and the judge calibration set, plus a dataset version, the licence and
the provenance line from `data/README.md`. `--check` runs in CI, so a changed corpus or a
retouched golden row is a visible, versioned event and never a silent one.

The dataset version is CalVer, `YYYY.MM.N`. Writing a manifest over changed files bumps
`N` unless `--dataset-version` says otherwise; unchanged files keep the version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DATA = Path("data")
MANIFEST_NAME = "MANIFEST.json"
PATTERNS = (
    "tickets.jsonl",
    "accounts.json",
    "policies/*.md",
    "policies/index.json",
    "golden/*.json",
    "golden/*.jsonl",
    "adversarial/*.jsonl",
)
LICENCE = (
    "Synthetic, generated for the course and for the cohort's use. No row of the reference "
    "dataset Tobi-Bueck/customer-support-tickets (CC BY-NC 4.0) is included."
)
INITIAL_VERSION = "2026.09.0"
_VERSION = re.compile(r"^(\d{4})\.(\d{2})\.(\d+)$")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rows_of(path: Path) -> int:
    """Records in the file: lines for JSONL, items for a JSON list, keys for a JSON object,
    non-empty lines for markdown."""
    if path.suffix == ".jsonl":
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return len(data) if isinstance(data, list | dict) else 1
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def provenance_line(readme: Path) -> str:
    """The first sentence under the README's provenance heading, or the README's first
    line when there is no such heading."""
    if not readme.exists():
        return ""
    lines = readme.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.lower().startswith("## provenance")), None
    )
    body = lines[start + 1 :] if start is not None else lines[1:]
    for line in body:
        if line.strip() and not line.startswith("#"):
            first = re.split(r"(?<=[.!?])\s", line.strip(), maxsplit=1)[0]
            return first
    return ""


def tracked_files(data_dir: Path) -> list[Path]:
    files: set[Path] = set()
    for pattern in PATTERNS:
        files.update(p for p in data_dir.glob(pattern) if p.is_file())
    return sorted(files)


def describe(data_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        p.relative_to(data_dir).as_posix(): {
            "sha256": sha256_of(p),
            "bytes": p.stat().st_size,
            "rows": rows_of(p),
        }
        for p in tracked_files(data_dir)
    }


def bump(version: str) -> str:
    m = _VERSION.match(version)
    now = datetime.now(UTC)
    if not m:
        return f"{now:%Y.%m}.0"
    year, month, n = m.groups()
    if (year, month) != (f"{now:%Y}", f"{now:%m}"):
        return f"{now:%Y.%m}.0"
    return f"{year}.{month}.{int(n) + 1}"


def load(data_dir: Path) -> dict[str, Any] | None:
    path = data_dir / MANIFEST_NAME
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def diff(manifest: dict[str, Any] | None, data_dir: Path) -> list[str]:
    """What changed since the manifest was written: one line per file, empty when nothing
    did."""
    if manifest is None:
        return [f"no {MANIFEST_NAME} in {data_dir}"]
    recorded = manifest.get("files", {})
    current = describe(data_dir)
    out = []
    for name in sorted(set(recorded) | set(current)):
        if name not in current:
            out.append(f"missing {name}")
        elif name not in recorded:
            out.append(f"untracked {name} (new file, not in the manifest)")
        elif recorded[name]["sha256"] != current[name]["sha256"]:
            out.append(
                f"changed {name}: {recorded[name]['rows']} rows to {current[name]['rows']} rows"
            )
    return out


def build(data_dir: Path, *, dataset_version: str | None = None) -> dict[str, Any]:
    previous = load(data_dir)
    files = describe(data_dir)
    if dataset_version is None:
        if previous is None:
            dataset_version = INITIAL_VERSION
        elif previous.get("files") != files:
            dataset_version = bump(previous.get("dataset_version", INITIAL_VERSION))
        else:
            dataset_version = previous.get("dataset_version", INITIAL_VERSION)
    return {
        "dataset_version": dataset_version,
        "written_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "licence": LICENCE,
        "provenance": provenance_line(data_dir / "README.md"),
        "files": files,
    }


def write(data_dir: Path, *, dataset_version: str | None = None) -> dict[str, Any]:
    manifest = build(data_dir, dataset_version=dataset_version)
    (data_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument(
        "--check", action="store_true", help="compare, do not write; exit 1 on a change"
    )
    ap.add_argument("--dataset-version", help="set the version instead of bumping it")
    args = ap.parse_args(argv)
    if args.check:
        changes = diff(load(args.data), args.data)
        if changes:
            print(f"{args.data / MANIFEST_NAME} is stale; run `python -m nw.data_manifest`:")
            for c in changes:
                print(f"  {c}")
            return 1
        manifest = load(args.data) or {}
        print(f"data matches the manifest, dataset {manifest.get('dataset_version')}")
        return 0
    manifest = write(args.data, dataset_version=args.dataset_version)
    print(
        f"wrote {args.data / MANIFEST_NAME}: dataset {manifest['dataset_version']}, "
        f"{len(manifest['files'])} files"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
