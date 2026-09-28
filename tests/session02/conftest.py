"""A deterministic ticket fixture with real signal, so training tests are fast and
do not depend on the generated corpus."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

TEMPLATES = {
    "P0": [
        (
            "URGENT production down",
            "Our entire workspace is down, all users get 502 errors since {t}. Production is blocked, this is critical.",
        ),
        (
            "Possible data breach",
            "We see another customer's records in our export. Security incident, please escalate immediately.",
        ),
        (
            "Data loss after sync",
            "The nightly sync deleted {n} records. Data loss across all dashboards, outage for everyone.",
        ),
    ],
    "P1": [
        (
            "SSO login failing for many users",
            "About {n} users cannot log in via SAML since {t}. Redirect loop after the IdP. Major impact.",
        ),
        (
            "Webhooks not delivered",
            "Webhook deliveries failed {n} times today with timeout errors. Our integration is broken.",
        ),
        (
            "Dashboard exports timing out",
            "PDF export fails for the sales dashboard, error 500 every time since {t}.",
        ),
    ],
    "P2": [
        (
            "Slow dashboard",
            "One dashboard takes {n} seconds to load. Others are fine. Not urgent but annoying.",
        ),
        (
            "Duplicate alert emails",
            "We receive two copies of each alert email. Started {t}. Could you check the routing?",
        ),
        (
            "Invoice question",
            "Invoice INV-{n} shows {n} seats but we have fewer. Can you correct it?",
        ),
    ],
    "P3": [
        (
            "How to export audit log",
            "Is there documentation on exporting the audit log to CSV? Thanks.",
        ),
        (
            "Feature request: dark mode",
            "It would be nice to have a dark mode in the mobile app. Feedback only.",
        ),
        (
            "Question about plan limits",
            "What is the widget limit per dashboard on the Pro plan? Just curious.",
        ),
    ],
}
WEIGHTS = {"P0": 0.04, "P1": 0.35, "P2": 0.41, "P3": 0.20}


def make_rows(n: int, seed: int = 1) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        p = rng.choices(list(WEIGHTS), weights=list(WEIGHTS.values()), k=1)[0]
        subj, body = rng.choice(TEMPLATES[p])
        body = body.format(
            t=rng.choice(["7am", "yesterday", "Monday", "last night"]), n=rng.randint(3, 900)
        )
        if rng.random() < 0.15:
            body = body.lower().replace(".", "")
        rows.append(
            {
                "ticket_id": f"T-{i:06d}",
                "account_id": f"NW-{10000 + i % 500:05d}",
                "subject": subj,
                "body": f"{body}\nRef {i}",  # unique text, so no duplicates or split leaks
                "priority": p,
                "split": "train" if i % 10 < 8 else ("val" if i % 10 == 8 else "test"),
                "language": "en",
            }
        )
    return rows


@pytest.fixture(scope="session")
def ticket_rows() -> list[dict]:
    return make_rows(1200)


REAL = Path(__file__).resolve().parents[2] / "data" / "tickets.jsonl"


@pytest.fixture(scope="session")
def ticket_file(tmp_path_factory, ticket_rows) -> Path:
    """The committed Northwind corpus when present (the acceptance numbers are about that
    data), otherwise the synthetic fixture so the suite still runs on a bare checkout."""
    if REAL.exists() and REAL.stat().st_size > 1_000_000:
        return REAL
    path = tmp_path_factory.mktemp("data") / "tickets.jsonl"
    with path.open("w") as f:
        for r in ticket_rows:
            f.write(json.dumps(r) + "\n")
    return path


@pytest.fixture(scope="session")
def trained(tmp_path_factory, ticket_file):
    from nw.triage.train import train

    out = tmp_path_factory.mktemp("artifacts")
    model, report = train(ticket_file, out)
    return model, report, out
