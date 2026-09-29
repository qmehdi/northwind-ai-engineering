"""Drift reports for the Local track: the training tickets as reference, each service's
capture file as current. Mirrors what the services compute for `/drift` (PSI on priority and
confidence) with Evidently's own tests, so the two can be compared in the UI on :8030.

Reference: data/tickets.jsonl (priority, text length). Current: the capture files the
services append to when NW_<SERVICE>_CAPTURE is set (priority, confidence, and for the policy
service top_confidence and refused). A capture file with fewer than NW_DRIFT_MIN rows is
reported as warming up and skipped.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd
from evidently import Report
from evidently.presets import DataDriftPreset
from evidently.ui.workspace import Workspace

REFERENCE = Path(os.environ.get("NW_TICKETS", "/data/tickets.jsonl"))
CAPTURES = {
    "triage": Path(os.environ.get("NW_TRIAGE_CAPTURE", "/artifacts/triage/predictions.jsonl")),
    "semantic": Path(
        os.environ.get("NW_SEMANTIC_CAPTURE", "/artifacts/semantic/predictions.jsonl")
    ),
    "policy": Path(os.environ.get("NW_POLICY_CAPTURE", "/artifacts/policy/capture.jsonl")),
}
WORKSPACE = os.environ.get("NW_EVIDENTLY_WORKSPACE", "/workspace")
REPORTS = Path(os.environ.get("NW_EVIDENTLY_REPORTS", "/reports"))
MIN_ROWS = int(os.environ.get("NW_DRIFT_MIN", "50"))


def load_jsonl(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return pd.DataFrame(rows)


def frame(df: pd.DataFrame, service: str) -> pd.DataFrame:
    out = pd.DataFrame()
    if "priority" in df:
        out["priority"] = df["priority"].astype(str)
    if service == "policy":
        if "top_confidence" in df:
            out["confidence"] = pd.to_numeric(df["top_confidence"], errors="coerce")
        if "refused" in df:
            out["refused"] = df["refused"].astype(bool).astype(int)
    elif "confidence" in df:
        out["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    if "body" in df:
        out["text_length"] = df["body"].astype(str).str.len()
    return out


def main() -> int:
    reference = load_jsonl(REFERENCE)
    if reference.empty:
        print(f"no reference at {REFERENCE}")
        return 1
    workspace = Workspace.create(WORKSPACE)
    projects = {p.name: p for p in workspace.list_projects()}
    REPORTS.mkdir(parents=True, exist_ok=True)
    written = 0
    for service, path in CAPTURES.items():
        current = load_jsonl(path)
        if len(current) < MIN_ROWS:
            print(f"{service}: {len(current)} rows in {path}, warming up (need {MIN_ROWS})")
            continue
        ref, cur = frame(reference, service), frame(current, service)
        columns = [c for c in cur.columns if c in ref.columns]
        if not columns:
            print(f"{service}: no shared columns with the reference")
            continue
        report = Report([DataDriftPreset()], include_tests=True)
        snapshot = report.run(cur[columns], ref[columns])
        project = projects.get(service) or workspace.create_project(
            service, description=f"Drift of the {service} service against the training tickets"
        )
        projects[service] = project
        workspace.add_run(project.id, snapshot)
        snapshot.save_html(str(REPORTS / f"{service}.html"))
        print(f"{service}: report over {len(cur)} rows, columns {columns}")
        written += 1
    print(f"{written} report(s) written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
