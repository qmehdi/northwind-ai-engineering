"""The pipeline tests reuse the Project 1 and Project 2 fixtures: the deterministic ticket
corpus, the tiny random encoder and its tokenizer. Nothing here reaches a cloud."""

from __future__ import annotations

from pathlib import Path

import pytest

from nw.platform.base import Tenant
from tests.pipelines.fake_registry import FakeRegistry
from tests.session02.conftest import make_rows, ticket_file, ticket_rows  # noqa: F401
from tests.session03.conftest import (  # noqa: F401
    config,
    s3_file,
    s3_rows,
    spec,
    tiny_tokenizer,
)


@pytest.fixture(scope="session")
def small_ticket_file(tmp_path_factory) -> Path:
    """The synthetic fixture on disk, always, so the end-to-end runs stay fast and the
    metrics they check are about this corpus."""
    import json

    path = tmp_path_factory.mktemp("pipelines") / "tickets.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in make_rows(1200)) + "\n")
    return path


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(name="alice", environment="northwind")


@pytest.fixture
def registry(tmp_path) -> FakeRegistry:
    return FakeRegistry(tmp_path / "registry")
