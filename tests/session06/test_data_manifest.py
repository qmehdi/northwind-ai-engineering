"""data/MANIFEST.json pins every data file by content; --check fails when one moves."""

import json
from pathlib import Path

import pytest

from nw import data_manifest as dm

pytestmark = pytest.mark.session06

README = """# Northwind data

Everything here is synthetic.

## Provenance and the priority rule

The schema is modelled on a public dataset. Northwind's levels are derived by a rule:
"""


@pytest.fixture
def data(tmp_path) -> Path:
    d = tmp_path / "data"
    (d / "policies").mkdir(parents=True)
    (d / "golden").mkdir()
    (d / "adversarial").mkdir()
    (d / "README.md").write_text(README)
    (d / "tickets.jsonl").write_text('{"a": 1}\n{"a": 2}\n\n')
    (d / "accounts.json").write_text(json.dumps([{"id": 1}, {"id": 2}, {"id": 3}]))
    (d / "policies" / "sla.md").write_text("# SLA\n\nUptime 99.9 percent.\n")
    (d / "policies" / "index.json").write_text(json.dumps([{"doc_id": "sla"}]))
    (d / "golden" / "policy_qa.jsonl").write_text('{"q": 1}\n')
    (d / "golden" / "baseline.json").write_text(json.dumps({"aggregate": {}, "cases": []}))
    (d / "adversarial" / "tickets.jsonl").write_text('{"id": "adv-01"}\n')
    (d / "notes.txt").write_text("not tracked")
    return d


def test_manifest_records_hashes_rows_licence_and_provenance(data):
    m = dm.write(data)
    assert m["dataset_version"] == dm.INITIAL_VERSION and m["licence"] == dm.LICENCE
    assert m["provenance"] == "The schema is modelled on a public dataset."
    files = m["files"]
    assert set(files) == {
        "tickets.jsonl",
        "accounts.json",
        "policies/sla.md",
        "policies/index.json",
        "golden/policy_qa.jsonl",
        "golden/baseline.json",
        "adversarial/tickets.jsonl",
    }
    assert files["tickets.jsonl"]["rows"] == 2 and files["accounts.json"]["rows"] == 3
    assert files["golden/baseline.json"]["rows"] == 2 and files["policies/sla.md"]["rows"] == 2
    assert len(files["tickets.jsonl"]["sha256"]) == 64
    assert json.loads((data / "MANIFEST.json").read_text()) == m


def test_check_passes_then_fails_on_a_changed_missing_or_new_file(data, capsys):
    dm.write(data)
    assert dm.main(["--data", str(data), "--check"]) == 0
    (data / "tickets.jsonl").write_text('{"a": 1}\n')
    (data / "golden" / "baseline.json").unlink()
    (data / "golden" / "new.json").write_text("{}")
    assert dm.main(["--data", str(data), "--check"]) == 1
    out = capsys.readouterr().out
    assert "changed tickets.jsonl: 2 rows to 1 rows" in out
    assert "missing golden/baseline.json" in out and "untracked golden/new.json" in out


def test_rewrite_bumps_the_version_only_when_data_changed(data):
    first = dm.write(data)
    assert dm.write(data)["dataset_version"] == first["dataset_version"]
    (data / "tickets.jsonl").write_text('{"a": 9}\n')
    bumped = dm.write(data)["dataset_version"]
    assert bumped != first["dataset_version"] and bumped == dm.bump(first["dataset_version"])
    assert dm.write(data, dataset_version="2030.01.0")["dataset_version"] == "2030.01.0"
    assert dm.main(["--data", str(data), "--check"]) == 0


def test_bump_is_calver():
    assert dm.bump("2026.09.3") in {"2026.09.4"} or dm.bump("2026.09.3").endswith(".0")
    assert dm.bump("garbage").count(".") == 2


def test_the_committed_manifest_matches_the_repository_data():
    assert dm.diff(dm.load(dm.DEFAULT_DATA), dm.DEFAULT_DATA) == []
    manifest = dm.load(dm.DEFAULT_DATA)
    names = set(manifest["files"])
    assert {"tickets.jsonl", "accounts.json", "adversarial/tickets.jsonl"} <= names
    assert {
        "golden/policy_qa.jsonl",
        "golden/baseline.json",
        "golden/agent_baseline.json",
        "golden/semantic_production.json",
        "golden/triage_production.json",
        "golden/judge_calibration.jsonl",
    } <= names
    assert sum(n.startswith("policies/") and n.endswith(".md") for n in names) >= 20
    assert manifest["files"]["tickets.jsonl"]["rows"] == 7950
    assert "Tobi-Bueck" in manifest["provenance"] and manifest["licence"]
