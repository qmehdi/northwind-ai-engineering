"""`build(settings)` wires every protocol and reads the gateway from NW_GATEWAY_URL."""

from __future__ import annotations

import pytest

from nw.config import Settings, Track
from nw.platform.base import (
    AgentRuntime,
    EndpointClient,
    ModelRegistry,
    PipelineRunner,
    PromptStore,
    VectorStore,
    platform_for,
)
from nw.platform.local import LocalConfig, NoopCompose, build

pytestmark = pytest.mark.session06


def test_build_returns_every_protocol(monkeypatch):
    pytest.importorskip("qdrant_client")
    monkeypatch.setenv("NW_GATEWAY_URL", "http://localhost:4000")
    settings = Settings(track=Track.LOCAL, _env_file=None)
    p = build(settings, compose=NoopCompose())
    assert p.track is Track.LOCAL and p.gateway_url == "http://localhost:4000"
    assert isinstance(p.registry, ModelRegistry)
    assert isinstance(p.pipelines, PipelineRunner)
    assert isinstance(p.endpoints, EndpointClient)
    assert isinstance(p.prompts, PromptStore)
    assert isinstance(p.vectors, VectorStore)
    assert isinstance(p.agents, AgentRuntime)
    d = p.describe()
    assert d["registry"] == "LocalModelRegistry" and d["gateway"] == "http://localhost:4000"


def test_config_from_env_reads_every_knob(monkeypatch):
    for k, v in {
        "NW_MLFLOW_URI": "http://mlflow.test:5000",
        "NW_QDRANT_URL": "http://qdrant.test:6333",
        "NW_ENDPOINT_URL": "http://proxy.test:8000",
        "NW_AGENT_RUNTIME_URL": "http://agent.test:8000",
        "NW_PIPELINE_ROOT": "/tmp/runs",
        "NW_KFP_RUNNER": "docker",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NW_GATEWAY_URL", raising=False)
    cfg = LocalConfig.from_env(None)
    assert (
        cfg.mlflow_uri == "http://mlflow.test:5000" and cfg.qdrant_url == "http://qdrant.test:6333"
    )
    assert (
        cfg.endpoint_url == "http://proxy.test:8000" and cfg.agent_url == "http://agent.test:8000"
    )
    assert str(cfg.pipeline_root) == "/tmp/runs" and cfg.kfp_runner == "docker"
    assert cfg.gateway_url is None


def test_platform_for_local_track_uses_this_module(monkeypatch):
    pytest.importorskip("qdrant_client")
    monkeypatch.delenv("NW_GATEWAY_URL", raising=False)
    p = platform_for(Settings(track=Track.LOCAL, _env_file=None))
    assert p.describe()["pipelines"] == "LocalPipelineRunner" and p.gateway_url is None
