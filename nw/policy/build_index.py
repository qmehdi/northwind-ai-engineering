"""Chunk the policy corpus, build the retrieval index, and write its manifest.

    uv run python -m nw.policy.build_index --corpus data/policies --out artifacts/policy
    uv run python -m nw.policy.build_index --check      # exit 1 when the index is stale

The manifest (`manifest.json`) records what the index was built from: the corpus hash, the
chunker parameters, the embedding and reranker names, the chunk count, the build time, the
prompt versions registered at build, and the drift baseline: the top-hit confidence
distribution over the golden questions, the golden set's refusal share, and the answer
lengths from the baseline run. The service compares itself with all of that at startup and
in every request window.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from nw.llm import prompts
from nw.policy.chunking import chunk_corpus, save_chunks
from nw.policy.manifest import corpus_sha, is_stale, read_manifest, write_manifest
from nw.policy.monitor import make_baseline
from nw.policy.retrieval import DEFAULT_RERANKER, PolicyIndex, Reranker, real_embeddings


def drift_baseline(
    index: PolicyIndex,
    golden: Path,
    baseline_run: Path | None,
    *,
    k: int = 8,
    capture: Path | None = None,
) -> dict[str, Any] | None:
    """Retrieve every golden question once (no model call) and record the top-hit confidence
    distribution and the set's refusal share; answer lengths come from the baseline run.

    With `capture`, a JSONL the service wrote under NW_POLICY_CAPTURE, the baseline is that
    traffic instead: once a week of real questions exists, it is a better picture of normal
    than the golden set's mix of answerable and unanswerable cases."""
    if capture is not None and capture.exists():
        rows = [
            json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines() if line
        ]
        rows = [r for r in rows if not r.get("cached")]
        if rows:
            b = make_baseline(
                [float(r.get("top_confidence", 0.0)) for r in rows],
                sum(1 for r in rows if r.get("refused")) / len(rows),
            )
            return b | {"source": f"capture:{capture}"}
    if not golden.exists():
        return None
    from nw.policy.evaluate import load_cases

    cases = load_cases(golden)
    confidences = []
    for c in cases:
        hits = index.retrieve(c.question, k=k, audience=c.audience)
        confidences.append(hits[0].confidence if hits else 0.0)
    refusal_rate = sum(1 for c in cases if c.must_refuse) / len(cases) if cases else 0.0
    lengths = None
    if baseline_run and baseline_run.exists():
        run = json.loads(baseline_run.read_text(encoding="utf-8"))
        # A legacy baseline (another model, another golden set) says nothing about this
        # service's answers: no length reference rather than a wrong one.
        if not run.get("legacy"):
            results = run.get("results", [])
            lengths = [len(r["answer"]) for r in results if not r.get("refused")] or None
    return make_baseline(confidences, refusal_rate, lengths) | {"source": f"golden:{golden}"}


def build(
    corpus: Path,
    out: Path,
    *,
    max_tokens: int = 350,
    overlap: int = 60,
    embeddings: Any | None = None,
    reranker: Reranker | None = None,
    reranker_name: str | None = DEFAULT_RERANKER,
    golden: Path = Path("data/golden/policy_qa.jsonl"),
    baseline_run: Path | None = Path("data/golden/baseline.json"),
    capture: Path | None = None,
) -> dict[str, Any]:
    """Build the index under `out` and return the manifest. Tests pass hash embeddings and the
    overlap reranker; the CLI uses the real models."""
    chunks = chunk_corpus(corpus, max_tokens=max_tokens, overlap_tokens=overlap)
    out.mkdir(parents=True, exist_ok=True)
    save_chunks(chunks, out / "chunks.jsonl")
    index = PolicyIndex(chunks, embeddings or real_embeddings(), reranker=reranker)
    index.save(out, out / "chunks.jsonl")
    prompts.load_known()
    manifest = {
        "corpus": str(corpus),
        "corpus_sha256_12": corpus_sha(corpus),
        "documents": len({c.doc_id for c in chunks}),
        "chunks": len(chunks),
        "current_chunks": sum(1 for c in chunks if c.current),
        "internal_chunks": sum(1 for c in chunks if c.audience == "internal"),
        "redactions": sum(c.redactions for c in chunks),
        "chunker": {"max_tokens": max_tokens, "overlap": overlap},
        "embedder": getattr(index.embeddings, "name", "unknown"),
        "reranker": getattr(reranker, "name", None) if reranker else reranker_name,
        "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "prompt_versions": prompts.versions(),
        "baseline": drift_baseline(index, golden, baseline_run, capture=capture),
    }
    write_manifest(out, manifest)
    # summary.json stays for the guide and the older tooling; the manifest is the record.
    summary = {
        k: manifest[k]
        for k in ("documents", "chunks", "current_chunks", "internal_chunks", "redactions")
    } | {"max_tokens": max_tokens, "overlap": overlap}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return manifest


def check(corpus: Path, out: Path) -> int:
    """0 when the manifest matches the corpus, 1 when stale or missing. For CI and for the
    image build: never bake an index built from an older corpus."""
    manifest = read_manifest(out)
    current = corpus_sha(corpus)
    if manifest is None:
        print(f"index stale: no manifest under {out}; run make index-policy")
        return 1
    if is_stale(manifest, corpus):
        print(
            f"index stale: corpus is {current}, index was built from "
            f"{manifest.get('corpus_sha256_12')} at {manifest.get('built_at')}; "
            "run make index-policy"
        )
        return 1
    print(
        f"index fresh: corpus {current}, {manifest['chunks']} chunks, "
        f"built {manifest.get('built_at')}, prompts {manifest.get('prompt_versions')}"
    )
    return 0


def track_baseline_run() -> Path | None:
    """This track's no-judge baseline, whose `results` carry the Workhorse's answers: the
    answer-length reference for the drift monitor. None until one is written (`make
    eval-policy-baseline`); the legacy `data/golden/baseline.json` is never used."""
    from nw.config import settings

    path = Path(f"data/golden/baselines/policy-{settings().track.value}-no_judge.json")
    return path if path.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, default=Path("data/policies"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/policy"))
    ap.add_argument("--max-tokens", type=int, default=350)
    ap.add_argument("--overlap", type=int, default=60)
    ap.add_argument("--golden", type=Path, default=Path("data/golden/policy_qa.jsonl"))
    ap.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="an evaluation report with answers, for the answer-length reference; default this "
        "track's no-judge baseline (data/golden/baselines/policy-<track>-no_judge.json)",
    )
    ap.add_argument(
        "--capture",
        type=Path,
        default=None,
        help="a NW_POLICY_CAPTURE file; its traffic becomes the drift baseline instead",
    )
    ap.add_argument("--no-rerank", dest="rerank", action="store_false")
    ap.add_argument(
        "--check", action="store_true", help="compare the manifest with the corpus; exit 1 if stale"
    )
    args = ap.parse_args()
    if args.check:
        return check(args.corpus, args.out)
    reranker = None
    if args.rerank:
        from nw.policy.retrieval import CrossEncoderReranker

        reranker = CrossEncoderReranker()
    manifest = build(
        args.corpus,
        args.out,
        max_tokens=args.max_tokens,
        overlap=args.overlap,
        reranker=reranker,
        golden=args.golden,
        baseline_run=args.baseline or track_baseline_run(),
        capture=args.capture,
    )
    print(json.dumps({k: v for k, v in manifest.items() if k != "baseline"}, indent=1))
    b = manifest["baseline"]
    if b:
        print(
            f"drift baseline from {b['source']}: {b['n']} questions, confidence p50 "
            f"{b['confidence_p50']:.3f}, refusal share {b['refusal_rate']:.3f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
