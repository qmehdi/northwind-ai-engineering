"""Chunk the policy corpus and build the retrieval index.

uv run python -m nw.policy.build_index --corpus data/policies --out artifacts/policy
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nw.policy.chunking import chunk_corpus, save_chunks
from nw.policy.retrieval import PolicyIndex, real_embeddings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, default=Path("data/policies"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/policy"))
    ap.add_argument("--max-tokens", type=int, default=350)
    ap.add_argument("--overlap", type=int, default=60)
    args = ap.parse_args()
    chunks = chunk_corpus(args.corpus, max_tokens=args.max_tokens, overlap_tokens=args.overlap)
    args.out.mkdir(parents=True, exist_ok=True)
    save_chunks(chunks, args.out / "chunks.jsonl")
    index = PolicyIndex(chunks, real_embeddings())
    index.save(args.out, args.out / "chunks.jsonl")
    summary = {
        "documents": len({c.doc_id for c in chunks}),
        "chunks": len(chunks),
        "current_chunks": sum(1 for c in chunks if c.current),
        "internal_chunks": sum(1 for c in chunks if c.audience == "internal"),
        "redactions": sum(c.redactions for c in chunks),
        "max_tokens": args.max_tokens,
        "overlap": args.overlap,
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
