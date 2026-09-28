"""AgentOps: trace review sampling. Recent runs, biased to the ones a person should read;
rows a person can label; labelled rows become candidate adversarial cases."""

import json
import sys
from pathlib import Path

import pytest

from nw.agent.review import read_rows, sample, to_case, to_cases, to_row, weight
from nw.agent.trace import ProposedAction, Termination, Trajectory

pytestmark = pytest.mark.session05

TASK = "Ticket T-200005 from account NW-11498\nSubject: URGENT: production down\n\nEvery user gets a 502."


def _pool(n_plain=36, n_capped=3, n_proposed=1) -> list[Trajectory]:
    ts = []
    for i in range(n_plain):
        ts.append(Trajectory(run_id=f"plain{i:02d}", agent="resolver", task=TASK, final="ok"))
    for i in range(n_capped):
        ts.append(
            Trajectory(
                run_id=f"cap{i:02d}", agent="resolver", task=TASK, terminated=Termination.MAX_STEPS
            )
        )
    for i in range(n_proposed):
        t = Trajectory(run_id=f"prop{i:02d}", agent="resolver", task=TASK, final="proposed")
        t.proposed_actions = [ProposedAction(tool="escalate", arguments={"tier": "x"}, step=1)]
        ts.append(t)
    return ts


def test_sampling_is_biased_to_non_answers_and_proposals():
    pool = _pool()
    assert weight(pool[0]) == 1.0 and weight(pool[-2]) == 4.0 and weight(pool[-1]) == 3.0
    picked = sample(pool, 10, seed=1)
    assert len(picked) == 10 and len({t.run_id for t in picked}) == 10
    interesting = []
    for seed in range(30):
        rows = sample(pool, 10, seed=seed)
        interesting.append(sum(1 for t in rows if not t.run_id.startswith("plain")) / 10)
    population_share = 4 / 40
    assert sum(interesting) / len(interesting) > 2 * population_share
    assert sample(pool, 100) == sample(pool, 100) or len(sample(pool, 100)) == 40


def test_rows_carry_the_labelling_fields():
    row = to_row(_pool(0, 0, 1)[0])
    assert row["label"] == "" and row["note"] == "" and row["terminated"] == "answer"
    assert row["proposed"] == ['escalate({"tier": "x"})'] and row["task"].startswith("Ticket")
    assert set(row) >= {"run_id", "task", "tools_called", "final", "terminated", "label", "note"}


def test_labelled_rows_become_candidate_cases():
    rows = [
        {**to_row(_pool(1, 0, 0)[0]), "label": "should_escalate", "note": "P0, no proposal"},
        {**to_row(_pool(1, 0, 0)[0]), "label": "ok"},
        {**to_row(_pool(1, 0, 0)[0]), "label": ""},
        {
            **to_row(_pool(1, 0, 0)[0]),
            "label": "wrong tier",
            "tools_called": ["escalate", "lookup_customer"],
        },
    ]
    cases = to_cases(rows)
    assert len(cases) == 2
    first, second = cases
    assert first["ticket_id"] == "T-200005" and first["account_id"] == "NW-11498"
    assert (
        first["subject"] == "URGENT: production down" and first["body"] == "Every user gets a 502."
    )
    assert first["expect"] == {"must_escalate": True} and first["note"] == "P0, no proposal"
    assert second["kind"] == "wrong_tier" and second["expect"]["must_not_escalate"] is True
    assert second["expect"]["expected_tools_any"] == ["escalate", "lookup_customer"]
    assert to_case({"label": "x", "task": "free text ticket"}, 1)["ticket_id"] == "T-000000"


def test_cli_sample_then_to_cases(tmp_path, monkeypatch, capsys):
    from nw.agent import review as cli

    traces = tmp_path / "traces"
    for t in _pool(5, 2, 1):
        t.save(traces)
    out = tmp_path / "review.jsonl"
    monkeypatch.setattr(
        sys,
        "argv",
        ["review", "--traces", str(traces), "--sample", "4", "--out", str(out), "--seed", "3"],
    )
    assert cli.main() == 0 and "4 of 8 runs sampled" in capsys.readouterr().out
    rows = read_rows(out)
    assert len(rows) == 4 and all(r["label"] == "" for r in rows)
    rows[0]["label"] = "should_escalate"
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    cases = tmp_path / "candidates.jsonl"
    monkeypatch.setattr(sys, "argv", ["review", "--to-cases", str(out), "--out", str(cases)])
    assert cli.main() == 0
    assert len(read_rows(cases)) == 1 and read_rows(cases)[0]["expect"]["must_escalate"] is True
    assert Path(cases).exists()
