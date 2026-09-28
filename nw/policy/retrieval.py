"""Hybrid retrieval over policy chunks: dense plus BM25, fused, then reranked.

Dense retrieval finds paraphrases; BM25 finds exact terms like "99.95" or
"sla-2023" that embeddings blur. Reciprocal rank fusion merges the two lists
without tuning a weight. A cross-encoder reranker then reads query and chunk
together and reorders the short list, which is where most of the quality
comes from and most of the latency goes.
"""

from __future__ import annotations

import json
import math
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from nw.policy.chunking import Chunk, load_chunks
from nw.policy.manifest import read_manifest

DEFAULT_RERANKER = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_TOKEN = re.compile(r"[a-z0-9][a-z0-9.\-]*")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@dataclass
class Retrieved:
    chunk: Chunk
    score: float
    retriever: str  # dense | bm25 | hybrid | reranked

    @property
    def confidence(self) -> float:
        """The score on one 0 to 1 scale whatever produced it, so a refusal threshold means
        the same thing in every retrieval mode: cosine for dense, a saturating BM25 map,
        reciprocal-rank fusion scaled so first place in both lists is about 1, and the
        cross-encoder logit through a sigmoid."""
        s = self.score
        if self.retriever == "dense":
            return max(0.0, min(1.0, s))
        if self.retriever == "bm25":
            return 1.0 - math.exp(-s / 5.0)
        if self.retriever == "hybrid":
            return max(0.0, min(1.0, s * 30.0))
        return 1.0 / (1.0 + math.exp(-s))


class Embeddings(Protocol):
    def encode(self, texts: list[str], batch_size: int = 64) -> np.ndarray: ...


class HashEmbeddings:
    """A deterministic stand-in for tests: hashed bag of words, unit normalised.
    Not semantic, but it makes dense retrieval exercisable with no model download."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.name = "hash"

    def encode(self, texts: list[str], batch_size: int = 64) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in tokenize(t):
                out[i, zlib.crc32(tok.encode()) % self.dim] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.maximum(norms, 1e-6)


class Reranker(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


class CrossEncoderReranker:
    def __init__(self, name: str = DEFAULT_RERANKER) -> None:
        from sentence_transformers import CrossEncoder

        self.name = name
        self.model = CrossEncoder(name)

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self.model.predict([(query, t) for t in texts])]


class OverlapReranker:
    """Test stand-in: token overlap between query and chunk."""

    name = "overlap"

    def score(self, query: str, texts: list[str]) -> list[float]:
        q = set(tokenize(query))
        return [len(q & set(tokenize(t))) / max(1, len(q)) for t in texts]


def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal rank fusion: each list contributes 1 / (k + rank)."""
    return {cid: 1.0 for cid in rankings[0]}  # Step 4: fuse both lists


class PolicyIndex:
    def __init__(
        self, chunks: list[Chunk], embeddings: Embeddings, *, reranker: Reranker | None = None
    ) -> None:
        from rank_bm25 import BM25Okapi

        self.chunks = chunks
        self.by_id = {c.id: c for c in chunks}
        self.embeddings = embeddings
        self.reranker = reranker
        self.vectors = embeddings.encode([c.text for c in chunks])
        self.bm25 = BM25Okapi([tokenize(c.text) for c in chunks])
        self.manifest: dict[str, Any] = {}  # filled by `load` from manifest.json when present

    @property
    def manifest_hash(self) -> str:
        """The corpus hash the index was built from, or "none" for an in-memory index."""
        return str(self.manifest.get("corpus_sha256_12") or "none")

    # ----- single retrievers ------------------------------------------------

    def dense(self, query: str, k: int) -> list[Retrieved]:
        q = self.embeddings.encode([query])[0]
        scores = self.vectors @ q
        top = np.argsort(-scores)[:k]
        return [Retrieved(self.chunks[i], float(scores[i]), "dense") for i in top]

    def lexical(self, query: str, k: int) -> list[Retrieved]:
        scores = self.bm25.get_scores(tokenize(query))
        top = np.argsort(-scores)[:k]
        return [Retrieved(self.chunks[i], float(scores[i]), "bm25") for i in top if scores[i] > 0]

    # ----- the pipeline -----------------------------------------------------

    def retrieve(
        self,
        query: str,
        *,
        k: int = 8,
        candidates: int = 30,
        hybrid: bool = True,
        rerank: bool = True,
        current_only: bool = True,
        audience: str = "customer",
    ) -> list[Retrieved]:
        """Candidates from both retrievers, fused, filtered by policy metadata, reranked, top k.

        Filtering happens after retrieval on purpose: a superseded document or an
        internal one may be the best lexical match, and the filter is the place where
        that is decided by metadata, not by luck.
        """
        return [r for r in self.dense(query, k) if self._allowed(r.chunk, True, audience)]

    @staticmethod
    def _allowed(chunk: Chunk, current_only: bool, audience: str) -> bool:
        if current_only and not chunk.current:
            return False
        if audience == "customer" and chunk.audience == "internal":
            return False
        return True

    # ----- persistence ------------------------------------------------------

    def save(self, directory: Path, chunks_path: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        np.save(directory / "vectors.npy", self.vectors)
        (directory / "metadata.json").write_text(
            json.dumps(
                {
                    "embedder": getattr(self.embeddings, "name", "unknown"),
                    "n": len(self.chunks),
                    "chunks": str(chunks_path),
                },
                indent=1,
            )
        )

    @classmethod
    def load(
        cls,
        directory: Path,
        embeddings: Embeddings,
        chunks_path: Path,
        *,
        reranker: Reranker | None = None,
    ) -> PolicyIndex:
        obj = cls.__new__(cls)
        from rank_bm25 import BM25Okapi

        obj.chunks = load_chunks(chunks_path)
        obj.by_id = {c.id: c for c in obj.chunks}
        obj.embeddings = embeddings
        obj.reranker = reranker
        obj.vectors = np.load(directory / "vectors.npy")
        obj.bm25 = BM25Okapi([tokenize(c.text) for c in obj.chunks])
        obj.manifest = read_manifest(directory) or {}
        return obj


def real_embeddings() -> Any:
    from nw.semantic.embed import Embedder

    return Embedder()


class S3VectorsDense:
    """The managed dense retriever on the AWS Reference stack: Amazon S3 Vectors.

    Shapes verified against the installed botocore model (s3vectors 2025-07-15):
    `query_vectors(vectorBucketName, indexName, topK, queryVector={"float32": [...]},
    returnMetadata=True, returnDistance=True)` returns `vectors[].key` and `distance`.
    Cosine distance is 1 minus similarity, so the score returned here is 1 - distance,
    the same scale as the in-process retriever.
    """

    def __init__(
        self, bucket: str, index: str, region: str, embeddings: Embeddings, *, client: Any = None
    ) -> None:
        import boto3

        self.bucket, self.index, self.embeddings = bucket, index, embeddings
        self.client = client or boto3.client("s3vectors", region_name=region)

    def publish(self, chunks: list[Chunk], batch: int = 100) -> int:
        vectors = self.embeddings.encode([c.text for c in chunks])
        for i in range(0, len(chunks), batch):
            self.client.put_vectors(
                vectorBucketName=self.bucket,
                indexName=self.index,
                vectors=[
                    {
                        "key": c.id,
                        "data": {"float32": [float(x) for x in vectors[j]]},
                        "metadata": {
                            "doc_id": c.doc_id,
                            "section": c.section,
                            "effective": c.effective,
                            "audience": c.audience,
                            "current": c.current,
                        },
                    }
                    for j, c in enumerate(chunks[i : i + batch], start=i)
                ],
            )
        return len(chunks)

    def query(self, text: str, k: int) -> list[tuple[str, float]]:
        q = self.embeddings.encode([text])[0]
        r = self.client.query_vectors(
            vectorBucketName=self.bucket,
            indexName=self.index,
            topK=k,
            queryVector={"float32": [float(x) for x in q]},
            returnMetadata=False,
            returnDistance=True,
        )
        return [(v["key"], 1.0 - float(v.get("distance", 0.0))) for v in r.get("vectors", [])]


class ManagedPolicyIndex(PolicyIndex):
    """PolicyIndex whose dense stage is a managed service. BM25, fusion, filters and the
    reranker are unchanged, so everything Session 4 taught still applies; only the
    vector store moved."""

    def __init__(
        self, chunks: list[Chunk], dense: S3VectorsDense, *, reranker: Reranker | None = None
    ) -> None:
        from rank_bm25 import BM25Okapi

        self.chunks = chunks
        self.by_id = {c.id: c for c in chunks}
        self.embeddings = dense.embeddings
        self.reranker = reranker
        self.vectors = None  # never materialised locally
        self.bm25 = BM25Okapi([tokenize(c.text) for c in chunks])
        self._dense = dense
        self.manifest = {}

    def dense(self, query: str, k: int) -> list[Retrieved]:
        return [
            Retrieved(self.by_id[cid], score, "dense")
            for cid, score in self._dense.query(query, k)
            if cid in self.by_id
        ]
