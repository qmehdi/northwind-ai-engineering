"""One refusal bar for every retriever: the confidence scale."""

import pytest

from nw.policy.answer import weak_retrieval
from nw.policy.chunking import Chunk
from nw.policy.retrieval import Retrieved

pytestmark = pytest.mark.session04


def _chunk() -> Chunk:
    return Chunk(
        id="d#1",
        doc_id="d",
        title="t",
        section="s",
        text="x",
        effective="2025-01-01",
        audience="customer",
    )


@pytest.mark.parametrize(
    "retriever, score, low, high",
    [
        ("dense", 0.8, 0.79, 0.81),
        ("dense", -0.2, 0.0, 0.0),
        ("reranked", 0.0, 0.49, 0.51),
        ("reranked", 4.0, 0.98, 1.0),
        ("hybrid", 2 / 61, 0.97, 1.0),
        ("bm25", 0.0, 0.0, 0.0),
    ],
)
def test_confidence_is_on_one_scale(retriever, score, low, high):
    c = Retrieved(_chunk(), score, retriever).confidence
    assert low <= c <= high


def test_threshold_means_the_same_in_every_mode():
    weak_dense = [Retrieved(_chunk(), 0.1, "dense")]
    weak_rerank = [Retrieved(_chunk(), -3.0, "reranked")]
    strong_rerank = [Retrieved(_chunk(), 3.0, "reranked")]
    assert weak_retrieval(weak_dense, 0.5) and weak_retrieval(weak_rerank, 0.5)
    assert not weak_retrieval(strong_rerank, 0.5)
    assert weak_retrieval([], 0.0)
