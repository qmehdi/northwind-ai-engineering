"""The index manifest: what an index was built from, so a service can tell when it is stale.

`artifacts/policy/manifest.json` is written by `build_index` and read by `PolicyIndex.load`
and the service. It carries the corpus hash, the chunker parameters, the embedding and
reranker model names, the chunk count, the build time, the prompt versions registered at
build time, and the drift baseline the service measures against. An index whose corpus hash
no longer matches `data/policies` is stale: the service says so on `/version`, in the gauge
`nw_policy_index_stale` and in one `index_stale` warning, and `build_index --check` exits 1
so a pipeline never bakes it into an image.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST = "manifest.json"
CORPUS_SUFFIXES = (".md", ".json")


def corpus_sha(corpus: Path) -> str:
    """The first twelve hex characters of the SHA-256 over every policy file, name and content,
    in sorted order. Renaming, editing, adding or removing a document all change it."""
    h = hashlib.sha256()
    for path in sorted(p for p in corpus.rglob("*") if p.suffix in CORPUS_SUFFIXES):
        h.update(path.relative_to(corpus).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:12]


def read_manifest(directory: Path) -> dict[str, Any] | None:
    path = directory / MANIFEST
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(directory: Path, manifest: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MANIFEST
    path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return path


def is_stale(manifest: dict[str, Any] | None, corpus: Path) -> bool | None:
    """True when the corpus on disk no longer hashes to what the manifest records. None when
    there is no corpus to compare with (a container that ships only the index)."""
    if manifest is None:
        return True
    if not corpus.exists():
        return None
    return manifest.get("corpus_sha256_12") != corpus_sha(corpus)
