"""The Model Monitor record preprocessor: a captured request becomes the `text_length` feature.

Model Monitor reads the endpoint's data capture (the request and the response of every
invocation) and compares it with the baseline. The request is a ticket (`subject`, `body`), not
a number, so a preprocessor script turns each captured record into the feature the baseline
describes. The monitoring schedule points at this file with `RecordPreprocessorSourceUri`
(the deployer in `deploy/aws/functions/deploy_on_approval` uploads it beside the baseline).

The contract is one function, `preprocess_handler(inference_record)`, returning a flat dict
(or a list of them for a batched request) whose keys are the feature names.
"""

from __future__ import annotations

import json
from typing import Any

FEATURE = "text_length"


def _payload(record: Any) -> Any:
    """The captured request body, whatever object shape the analyzer hands over."""
    endpoint_input = getattr(record, "endpoint_input", None)
    if endpoint_input is None and isinstance(record, dict):
        endpoint_input = record.get("endpoint_input") or record.get("endpointInput")
    data = getattr(endpoint_input, "data", None)
    if data is None and isinstance(endpoint_input, dict):
        data = endpoint_input.get("data")
    if data is None:
        data = record
    if isinstance(data, bytes | bytearray):
        data = data.decode("utf-8")
    if isinstance(data, str):
        try:
            return json.loads(data)
        except ValueError:
            return {"body": data}
    return data


def _feature(ticket: dict[str, Any]) -> dict[str, int]:
    return {FEATURE: len(str(ticket.get("subject") or "")) + len(str(ticket.get("body") or ""))}


def preprocess_handler(
    inference_record: Any, logger: Any = None
) -> dict[str, int] | list[dict[str, int]]:
    payload = _payload(inference_record)
    if isinstance(payload, dict) and "instances" in payload:
        payload = payload["instances"]
    if isinstance(payload, list):
        return [_feature(t) for t in payload if isinstance(t, dict)]
    if isinstance(payload, dict):
        return _feature(payload)
    return {FEATURE: 0}
