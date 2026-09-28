"""AgentOps in the service: the kill switch stops the spend without failing readiness, the
concurrency cap turns the fifth run into a 429, the drift window alerts on capped runs and
on a cost shift against the baseline, and NW_AGENT_CAPTURE keeps a line per run."""

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from nw.agent.loop import scripted_completion
from nw.agent.monitor import COST_BINS, STEP_BINS, AgentMonitor, RunSummary, histogram, psi
from nw.agent.trace import Step, Termination, Trajectory
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05

TASK = {"task": "Ticket T-200001 from account NW-10007: export is slow."}


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_kill_switch_refuses_runs_and_leaves_readiness_alone(make_agent_app, monkeypatch):
    app = make_agent_app(FakeProvider([scripted_completion("Answer.")]))
    monkeypatch.setenv("NW_AGENT_DISABLED", "1")
    rejected0 = _sample("nw_agent_rejected_total", role="resolver", reason="disabled")
    runs0 = _sample("nw_agent_runs_total", role="resolver", terminated="answer")
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 200
        r = c.post("/run", json=TASK)
        assert r.status_code == 503 and "NW_AGENT_DISABLED" in r.json()["detail"]
        assert c.get("/version").json()["disabled"] is True
        assert _sample("nw_agent_disabled", role="resolver") == 1.0
        monkeypatch.delenv("NW_AGENT_DISABLED")
        assert c.post("/run", json=TASK).status_code == 200
    assert _sample("nw_agent_rejected_total", role="resolver", reason="disabled") == rejected0 + 1
    assert _sample("nw_agent_runs_total", role="resolver", terminated="answer") == runs0 + 1


async def test_concurrency_cap_returns_429_beyond_the_limit(make_agent_app, monkeypatch):
    monkeypatch.setenv("NW_AGENT_MAX_CONCURRENT_RUNS", "1")
    app = make_agent_app(FakeProvider([scripted_completion("Answer.")], delay_s=0.2))
    rejected0 = _sample("nw_agent_rejected_total", role="resolver", reason="concurrency")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent"
    ) as c:
        first, second = await asyncio.gather(c.post("/run", json=TASK), c.post("/run", json=TASK))
        assert sorted([first.status_code, second.status_code]) == [200, 429]
        refused = first if first.status_code == 429 else second
        assert "NW_AGENT_MAX_CONCURRENT_RUNS" in refused.json()["detail"]
        assert (await c.post("/run", json=TASK)).status_code == 200  # the slot is free again
    assert (
        _sample("nw_agent_rejected_total", role="resolver", reason="concurrency") == rejected0 + 1
    )


def _traj(steps, cost, terminated=Termination.ANSWER, errors=0):
    t = Trajectory(run_id="r", agent="resolver", task="t", cost_usd=cost, terminated=terminated)
    t.steps = [Step(index=i, tool="search_policies", ok=i >= errors) for i in range(steps)]
    return t


def test_psi_histogram_and_the_monitor_bars():
    assert psi([0.25] * 4, [0.25] * 4) == pytest.approx(0.0)
    assert psi([0.7, 0.2, 0.1], [0.1, 0.2, 0.7]) > 0.5
    assert histogram([1, 2, 2, 9], STEP_BINS)[1] == 0.25 and sum(histogram([0.11], COST_BINS)) == 1
    baseline = {"steps": [4, 5, 4, 4, 7, 7, 4, 4, 8, 6, 6, 3, 4, 4, 4], "costs": [0.1] * 15}
    m = AgentMonitor(baseline, window=50, min_window=5)
    assert m.enabled and m.snapshot().level == "warming_up"
    for _ in range(10):
        m.observe(_traj(5, 0.11))
    snap = m.snapshot()
    assert snap.level in {"ok", "watch"} and snap.cap_rate == 0 and snap.error_rate == 0
    assert snap.steps_psi is not None and snap.cost_psi is not None and snap.cost_psi < 0.2
    for _ in range(40):
        m.observe(_traj(5, 0.39))  # same steps, four times the cost
    snap = m.snapshot()
    assert snap.level == "alert" and any("cost per run PSI" in r for r in snap.reasons)
    m = AgentMonitor(None, window=50, min_window=5)
    for _ in range(6):
        m.observe(_traj(10, 0.4, Termination.MAX_STEPS, errors=3))
    snap = m.snapshot()
    assert not m.enabled and snap.steps_psi is None
    assert snap.cap_rate == 1.0 and snap.error_rate == pytest.approx(0.3)
    assert snap.termination_share["max_steps"] == 1.0 and snap.level == "alert"
    assert any("cap rate" in r for r in snap.reasons) and any(
        "tool error" in r for r in snap.reasons
    )
    s = RunSummary.from_trajectory(_traj(3, 0.01, errors=1))
    assert (s.tool_calls, s.tool_errors, s.steps) == (3, 1, 3)


def test_service_drift_endpoint_gauges_alert_line_and_capture(make_agent_app, tmp_path, caplog):
    baseline = {"steps": [4, 5, 4, 4, 7, 7, 4, 4, 8, 6, 6, 3, 4, 4, 4], "costs": [0.1] * 15}
    capture = tmp_path / "runs.jsonl"
    app = make_agent_app(
        FakeProvider(
            lambda msgs, kw: scripted_completion("again", [("find_similar_tickets", {"body": "x"})])
        ),
        monitor=AgentMonitor(baseline, window=50, min_window=4),
        capture=capture,
        baseline=baseline,
    )
    with TestClient(app) as c, caplog.at_level(logging.WARNING, logger="nw.agent.service"):
        assert c.get("/drift").json()["level"] == "warming_up"
        for _ in range(5):
            r = c.post("/run", json={**TASK, "max_steps": 2})
            assert r.status_code == 200 and r.json()["terminated"] == "max_steps"
        snap = c.get("/drift").json()
        text = c.get("/metrics").text
    assert snap["window"] == 5 and snap["cap_rate"] == 1.0 and snap["level"] == "alert"
    assert snap["termination_share"]["max_steps"] == 1.0 and snap["steps_psi"] is not None
    assert _sample("nw_agent_drift_cap_rate") == 1.0 and _sample("nw_agent_drift_level") == 2.0
    assert 'nw_agent_drift_psi{feature="steps"}' in text
    assert any(r.getMessage() == "drift_alert" for r in caplog.records)
    lines = [json.loads(line) for line in capture.read_text().splitlines()]
    assert len(lines) == 5 and lines[0]["terminated"] == "max_steps" and lines[0]["agent_version"]
    assert lines[0]["tool_calls"] == 2 and lines[0]["steps"] == 2
