"""Publish the policy chunks to the AWS platform's managed retriever, S3 Vectors.

The in-process retriever ships the vectors inside the image (`artifacts/policy/vectors.npy`).
An S3 Vectors index of your own keeps them outside the image instead, so the index has to be
filled once after it exists. The AWS platform's knowledge base ingests the corpus itself
(`NW_RETRIEVER=knowledge-base`); this module is the direct path, run by hand. It embeds
the chunks with the same model the index was sized for and upserts them by id, so running it
twice is harmless.

    uv run python -m nw.policy.publish_vectors --bucket northwind-policy-<account>-<region>
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from nw.logging import configure_logging, get_logger, log_fields
from nw.policy.chunking import load_chunks
from nw.policy.retrieval import Embeddings, S3VectorsDense

log = get_logger("nw.policy.publish_vectors")


def publish(
    bucket: str,
    index: str,
    region: str,
    chunks_path: Path,
    *,
    embeddings: Embeddings | None = None,
    client: Any = None,
) -> int:
    chunks = load_chunks(chunks_path)
    if embeddings is None:
        from nw.policy.retrieval import real_embeddings

        embeddings = real_embeddings()
    dense = S3VectorsDense(bucket, index, region, embeddings, client=client)
    n = dense.publish(chunks)
    log.info("vectors published", extra=log_fields(bucket=bucket, index=index, chunks=n))
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", default=os.environ.get("NW_VECTOR_BUCKET"), required=False)
    parser.add_argument("--index", default=os.environ.get("NW_VECTOR_INDEX", "policy-chunks"))
    parser.add_argument("--region", default=os.environ.get("NW_AWS_REGION", "us-east-1"))
    parser.add_argument(
        "--chunks",
        type=Path,
        default=Path(os.environ.get("NW_POLICY_INDEX", "artifacts/policy")) / "chunks.jsonl",
    )
    args = parser.parse_args(argv)
    if not args.bucket:
        parser.error("--bucket or NW_VECTOR_BUCKET is required")
    configure_logging(os.environ.get("NW_LOG_FORMAT", "text"))
    n = publish(args.bucket, args.index, args.region, args.chunks)
    print(f"published {n} chunks to s3vectors://{args.bucket}/{args.index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
