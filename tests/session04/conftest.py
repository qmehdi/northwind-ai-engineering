"""A four-document synthetic policy corpus with one superseded pair and one internal doc."""

from __future__ import annotations

from pathlib import Path

import pytest

DOCS = {
    "sla-2025": """---
title: Service Level Agreement
doc_id: sla-2025
audience: customer
effective: 2025-03-01
supersedes: sla-2023
---
# Service Level Agreement

## Uptime commitments

Starter 99.5 percent, Pro 99.9 percent, Enterprise 99.95 percent, measured monthly.

## Response times

P0 is acknowledged within 15 minutes for Enterprise, 1 hour for Pro and 4 hours for Starter.

## Service credits

Credits are 5, 10 or 25 percent of the monthly fee and must be claimed within 30 days.
""",
    "sla-2023": """---
title: Service Level Agreement
doc_id: sla-2023
audience: customer
effective: 2023-01-15
supersedes: none
---
# Service Level Agreement

## Uptime commitments

Starter 99.0 percent, Pro 99.5 percent, Enterprise 99.9 percent, measured monthly.

## Response times

P0 is acknowledged within 30 minutes for Enterprise, 2 hours for Pro and 8 hours for Starter.

## Service credits

Credits are 5 or 10 percent of the monthly fee and must be claimed within 15 days.
""",
    "refunds": """---
title: Refunds and Billing Disputes
doc_id: refunds
audience: customer
effective: 2025-06-01
supersedes: none
---
# Refunds and Billing Disputes

## Duplicate charges

A duplicate charge is refunded in full to the original payment method within 5 business days.

## Annual plans

Annual plans are refundable pro rata within 30 days of renewal. Monthly plans are not refundable.

## Contact

Disputes go to billing@northwind.example or account NW-10007 in the portal.
""",
    "escalation-internal": """---
title: Support Escalation Matrix
doc_id: escalation-internal
audience: internal
effective: 2025-03-01
supersedes: none
---
# Support Escalation Matrix

## Internal only

This document is internal and must not be quoted to customers.

## Duty manager rule

A P0 must be acknowledged by a duty manager within 15 minutes. The on-call phone is +1 555 0100 2200.
""",
}


@pytest.fixture(scope="session")
def corpus_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("policies")
    for name, text in DOCS.items():
        (d / f"{name}.md").write_text(text)
    return d
