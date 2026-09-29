"""Fixtures for the platform tests. Everything offline runs on MLflow over a temporary sqlite
file with a local artifact root, Qdrant in memory and the kfp SubprocessRunner; `live` tests
need the compose stack (`make local-up`) and skip when its MLflow does not answer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nw.platform.base import Tenant
from nw.platform.local import NoopCompose

LIVE_MLFLOW = os.environ.get("NW_MLFLOW_URI", "http://localhost:5001")


@pytest.fixture
def tenant() -> Tenant:
    return Tenant("alice", "northwind")


@pytest.fixture
def mlflow_uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A tracking and registry store on a temporary sqlite file, artifacts under it.
    MLflow with a sqlite backend writes `mlruns/` relative to the working directory for
    artifacts, so the test runs from the temporary directory."""
    pytest.importorskip("mlflow")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MLFLOW_DISABLE_AGENT_HINT", "1")
    return f"sqlite:///{tmp_path / 'mlflow.db'}"


@pytest.fixture
def compose() -> NoopCompose:
    return NoopCompose()


@pytest.fixture(scope="session")
def live_stack() -> str:
    """The base URL of the stack's MLflow, or a skip when it is down."""
    import httpx

    try:
        r = httpx.get(f"{LIVE_MLFLOW}/health", timeout=3.0)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"local stack is down ({exc.__class__.__name__}); run make local-up")
    if r.status_code != 200:
        pytest.skip(f"local stack MLflow answered {r.status_code}")
    return LIVE_MLFLOW
