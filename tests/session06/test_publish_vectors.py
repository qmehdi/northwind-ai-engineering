"""Acceptance: the deploy script's publish step fills the managed index from the same
chunks file the Session path bakes into the image."""

import pytest

from nw.policy.chunking import chunk_corpus, save_chunks
from nw.policy.publish_vectors import publish
from nw.policy.retrieval import HashEmbeddings
from tests.session06.test_screen_and_vectors import FakeS3Vectors

pytestmark = pytest.mark.session06


def test_publish_upserts_every_chunk_by_id(corpus_dir, tmp_path):
    chunks = chunk_corpus(corpus_dir)
    path = tmp_path / "chunks.jsonl"
    save_chunks(chunks, path)
    fake = FakeS3Vectors()
    n = publish(
        "northwind-policy",
        "policy-chunks",
        "us-east-1",
        path,
        embeddings=HashEmbeddings(),
        client=fake,
    )
    assert n == len(chunks) and set(fake.store) == {c.id for c in chunks}
    assert (
        publish(
            "northwind-policy",
            "policy-chunks",
            "us-east-1",
            path,
            embeddings=HashEmbeddings(),
            client=fake,
        )
        == n
    )
