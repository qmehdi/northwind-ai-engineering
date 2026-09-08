"""The similarity index: past tickets embedded once, searched by cosine.

Session 5's agent calls `find_similar_tickets`; Session 4 reuses the same
embedding model for the policy corpus, so one model serves both. The index
is a versioned artifact built by a script, never at request time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from nw.triage.features import ticket_text

DEFAULT_EMBEDDER = "sentence-transformers/all-MiniLM-L6-v2"


class Embedder:
    def __init__(self, name: str = DEFAULT_EMBEDDER) -> None:
        from sentence_transformers import SentenceTransformer

        self.name = name
        self.model = SentenceTransformer(name)

    def encode(self, texts: list[str], batch_size: int = 64) -> np.ndarray:
        v = self.model.encode(
            texts, batch_size=batch_size, normalize_embeddings=True, convert_to_numpy=True
        )
        return v.astype(np.float32)


class TicketIndex:
    """Flat inner-product index over unit vectors, which is cosine similarity."""

    def __init__(self, vectors: np.ndarray, ids: list[str], meta: list[dict[str, Any]]) -> None:
        import faiss

        self.dim = vectors.shape[1]
        self.index = faiss.IndexFlatIP(self.dim)
        self.index.add(vectors)
        self.ids = ids
        self.meta = meta

    def search(
        self, query: np.ndarray, k: int = 5
    ) -> list[list[tuple[str, float, dict[str, Any]]]]:
        scores, idx = self.index.search(query.astype(np.float32), k)
        out = []
        for row_s, row_i in zip(scores, idx, strict=True):
            out.append(
                [
                    (self.ids[i], float(s), self.meta[i])
                    for s, i in zip(row_s, row_i, strict=True)
                    if i >= 0
                ]
            )
        return out

    def save(self, directory: Path) -> None:
        import faiss

        directory.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(directory / "tickets.faiss"))
        (directory / "ids.json").write_text(json.dumps(self.ids))
        (directory / "meta.jsonl").write_text("\n".join(json.dumps(m) for m in self.meta))

    @classmethod
    def load(cls, directory: Path) -> TicketIndex:
        import faiss

        obj = cls.__new__(cls)
        obj.index = faiss.read_index(str(directory / "tickets.faiss"))
        obj.dim = obj.index.d
        obj.ids = json.loads((directory / "ids.json").read_text())
        obj.meta = [
            json.loads(line) for line in (directory / "meta.jsonl").read_text().splitlines() if line
        ]
        return obj


def build_index(
    rows: list[dict[str, Any]],
    embedder: Embedder | None = None,
    *,
    vectors: np.ndarray | None = None,
) -> TicketIndex:
    """Embed every ticket's text; keep only what a search result needs as metadata."""
    texts = [ticket_text(r.get("subject", ""), r.get("body", "")) for r in rows]
    if vectors is None:
        vectors = (embedder or Embedder()).encode(texts)
    meta = [
        {
            "subject": r.get("subject", ""),
            "priority": r.get("priority"),
            "queue": r.get("queue"),
            "tags": r.get("tags", []),
            "answer": (r.get("answer") or "")[:500],
        }
        for r in rows
    ]
    return TicketIndex(vectors, [r["ticket_id"] for r in rows], meta)


def main() -> int:
    import argparse

    from nw.semantic.data import load_rows

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/index"))
    ap.add_argument("--model", default=DEFAULT_EMBEDDER)
    args = ap.parse_args()
    rows = [r for r in load_rows(args.data) if r.get("split") == "train"]
    index = build_index(rows, Embedder(args.model))
    index.save(args.out)
    (args.out / "metadata.json").write_text(
        json.dumps({"embedder": args.model, "n": len(rows), "dim": index.dim}, indent=1)
    )
    print(f"indexed {len(rows)} tickets, dim {index.dim}, into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
