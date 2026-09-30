"""The capstone endpoint honours the operator controls: the kill switch and the concurrency
cap sit in front of /route as they do in front of /run, and /version names both models."""

import pytest
from fastapi.testclient import TestClient

from nw.agent.loop import scripted_completion
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session06

ROUTE = {
    "ticket_id": "T-200001",
    "account_id": "NW-10007",
    "subject": "Slow export",
    "task": "Export takes 40 seconds.",
}


def test_route_is_refused_by_the_kill_switch_and_readiness_stays_up(make_agent_app, monkeypatch):
    app = make_agent_app(FakeProvider([scripted_completion("Check the export.")]))
    monkeypatch.setenv("NW_AGENT_DISABLED", "true")
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 200
        r = c.post("/route", json=ROUTE)
        assert r.status_code == 503 and "readiness is unchanged" in r.json()["detail"]
        monkeypatch.delenv("NW_AGENT_DISABLED")
        r = c.post("/route", json=ROUTE)
        assert r.status_code == 200 and r.json()["terminated"] == "answer"
        info = c.get("/version").json()
        assert info["economy_model"] == "fake-economy" and info["models"] == {
            "workhorse": "fake-workhorse"
        }
        snap = c.get("/drift").json()
        assert snap["window"] == 1 and snap["termination_share"]["answer"] == 1.0


def test_p0_route_records_a_version_without_a_model(make_agent_app):
    app = make_agent_app(FakeProvider([scripted_completion("should not be called")]))
    with TestClient(app) as c:
        r = c.post(
            "/route",
            json={**ROUTE, "subject": "Production down", "task": "The API is down for everyone."},
        )
        assert r.status_code == 200 and r.json()["cost_usd"] == 0.0
    from nw.agent import service
    from nw.agent.trace import Trajectory

    (path,) = service.state.trace_dir.glob("route-T-200001-*.json")  # one file per routed run
    t = Trajectory.load(path)
    assert t.agent == "router" and t.agent_version and len(t.agent_version) == 12
