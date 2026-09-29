"""The weighted proxy: the upstream file the canary is driven by, and the endpoint client."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from nw.platform.base import EndpointClient, Stage
from nw.platform.local import (
    LocalEndpointClient,
    LocalModelRegistry,
    Weights,
    parse_upstream,
    render_upstream,
)

pytestmark = pytest.mark.session06


@pytest.mark.parametrize("pct", [0, 1, 10, 50, 99, 100])
def test_render_parse_round_trip(pct: int):
    text = render_upstream(pct)
    w = parse_upstream(text)
    assert w == Weights(stable=100 - pct, canary=pct)
    assert "serving-stable:8000" in text and "serving-canary:8000" in text


def test_zero_weight_is_written_as_down_never_zero():
    text = render_upstream(0)
    assert "weight=0" not in text and "serving-canary:8000 weight=1 down" in text
    text = render_upstream(100)
    assert "serving-stable:8000 weight=1 down" in text


def test_render_rejects_out_of_range():
    with pytest.raises(ValueError):
        render_upstream(101)
    with pytest.raises(ValueError):
        render_upstream(-1)


def test_parse_rejects_incomplete_blocks():
    with pytest.raises(ValueError):
        parse_upstream("upstream serving { server serving-stable:8000 weight=1; }")
    with pytest.raises(ValueError):
        parse_upstream(render_upstream(0).replace("weight=100", "weight=1 down"))


def test_shipped_upstream_files_parse():
    root = Path(__file__).resolve().parents[2] / "deploy" / "local" / "proxy"
    assert parse_upstream((root / "upstream.conf").read_text()) == Weights(100, 0)
    higher = (root / "higher" / "upstream.conf").read_text()
    assert parse_upstream(higher) == Weights(100, 0) and "serving-stable-higher" in higher


def test_deploy_moves_aliases_weights_and_restarts(mlflow_uri, tenant, tmp_path, compose):
    registry = LocalModelRegistry(mlflow_uri)
    art = tmp_path / "art"
    art.mkdir()
    (art / "metadata.json").write_text(json.dumps({"version": "v1"}))
    v = registry.register(tenant, "widget", art, {}, {})
    proxy_dir = tmp_path / "proxy"
    client = LocalEndpointClient(registry, proxy_dir, "http://proxy.test", compose)
    assert isinstance(client, EndpointClient)
    url = client.deploy(tenant, v, canary_percent=10)
    assert url == "http://proxy.test/invocations"
    assert client.weights() == Weights(90, 10)
    assert registry.by_alias(tenant, "widget", "canary").version == v.version
    assert ("restart", "serving-canary") in compose.calls
    assert ("exec", "-T", "proxy", "nginx", "-s", "reload") in compose.calls
    client.deploy(tenant, v, live=True)
    assert registry.live(tenant, "widget").version == v.version
    assert client.weights() == Weights(100, 0)
    assert ("restart", "serving-stable") in compose.calls
    assert json.loads((proxy_dir / "weights.json").read_text()) == {"stable": 100, "canary": 0}
    client.delete(tenant, "widget")
    assert registry.by_alias(tenant, "widget", "canary") is None
    assert registry.versions(tenant, "widget")[0].stage is Stage.LIVE


def test_invoke_unwraps_predictions(monkeypatch, mlflow_uri, tenant, tmp_path, compose):
    registry = LocalModelRegistry(mlflow_uri)
    client = LocalEndpointClient(registry, tmp_path, "http://proxy.test", compose)
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"], seen["json"], seen["headers"] = url, json, headers
        return httpx.Response(
            200,
            json={"predictions": [{"priority": "P1"}]},
            headers={"x-upstream": "10.0.0.2:8000"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    out = client.invoke(tenant, "widget", {"subject": "s", "body": "b"})
    assert out["prediction"] == {"priority": "P1"} and out["replica"] == "10.0.0.2:8000"
    assert seen["json"] == {"dataframe_records": [{"subject": "s", "body": "b"}]}
    assert seen["headers"]["X-Tenant"] == "northwind-alice"
