"""Snapshot every service's OpenAPI spec, and refuse a breaking change in CI.

    uv run python scripts/openapi_snapshot.py            # write docs/openapi/<service>.json
    uv run python scripts/openapi_snapshot.py --check    # exit 1 on a breaking change

The committed snapshot is the API contract. `--check` regenerates each spec from the app
and compares: a removed path or operation, a removed schema or property, a property that
changed type, a required field that appeared or disappeared, a parameter removed or made
required, all fail. Anything additive (a new path, a new optional field, a longer
description) passes with a note to refresh the snapshot. Bumping the API version is the
way to make a breaking change: mount the new routes under `/v2` and the check is happy.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any

SERVICES = {
    "triage": "nw.triage.service:app",
    "semantic": "nw.semantic.service:app",
    "policy": "nw.policy.service:app",
    "agent": "nw.agent.service:app",
}
DEFAULT_DIR = Path("docs/openapi")


def load_app(spec: str) -> Any:
    module, attr = spec.split(":")
    return getattr(importlib.import_module(module), attr)


def snapshot(app: Any) -> dict[str, Any]:
    return json.loads(json.dumps(app.openapi()))


def _type_signature(schema: dict[str, Any]) -> str:
    """One string for the type of a property: `type`, or `$ref`, or the union of
    alternatives, with array item and enum values folded in."""
    if not isinstance(schema, dict):
        return json.dumps(schema, sort_keys=True)
    parts: list[str] = []
    if "$ref" in schema:
        parts.append(schema["$ref"])
    if "type" in schema:
        t = schema["type"]
        parts.append(",".join(t) if isinstance(t, list) else str(t))
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            parts.append(key + "[" + "|".join(_type_signature(s) for s in schema[key]) + "]")
    if "items" in schema:
        parts.append("items<" + _type_signature(schema["items"]) + ">")
    if "enum" in schema:
        parts.append("enum" + json.dumps(sorted(map(str, schema["enum"]))))
    if "format" in schema:
        parts.append("format:" + str(schema["format"]))
    return " ".join(parts) or "any"


def breaking_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Human-readable reasons the new spec breaks a client of the old one. Empty means
    the change is additive."""
    problems: list[str] = []
    old_paths, new_paths = old.get("paths", {}), new.get("paths", {})
    for path, ops in old_paths.items():
        if path not in new_paths:
            problems.append(f"removed path {path}")
            continue
        for method, op in ops.items():
            if method not in new_paths[path]:
                problems.append(f"removed operation {method.upper()} {path}")
                continue
            new_op = new_paths[path][method]
            old_params = {(p["name"], p["in"]): p for p in op.get("parameters", [])}
            new_params = {(p["name"], p["in"]): p for p in new_op.get("parameters", [])}
            for key, p in old_params.items():
                if key not in new_params:
                    problems.append(
                        f"removed parameter {key[1]}:{key[0]} on {method.upper()} {path}"
                    )
                elif new_params[key].get("required", False) and not p.get("required", False):
                    problems.append(
                        f"parameter {key[1]}:{key[0]} became required on {method.upper()} {path}"
                    )
            for key, p in new_params.items():
                if key not in old_params and p.get("required", False):
                    problems.append(
                        f"new required parameter {key[1]}:{key[0]} on {method.upper()} {path}"
                    )
            if "requestBody" in op and "requestBody" not in new_op:
                problems.append(f"removed request body on {method.upper()} {path}")
            for status in op.get("responses", {}):
                if status not in new_op.get("responses", {}):
                    problems.append(f"removed response {status} on {method.upper()} {path}")
    old_schemas = old.get("components", {}).get("schemas", {})
    new_schemas = new.get("components", {}).get("schemas", {})
    for name, schema in old_schemas.items():
        if name not in new_schemas:
            problems.append(f"removed schema {name}")
            continue
        new_schema = new_schemas[name]
        old_props, new_props = schema.get("properties", {}), new_schema.get("properties", {})
        for prop, definition in old_props.items():
            if prop not in new_props:
                problems.append(f"removed property {name}.{prop}")
            elif _type_signature(definition) != _type_signature(new_props[prop]):
                problems.append(
                    f"changed type of {name}.{prop}: "
                    f"{_type_signature(definition)} to {_type_signature(new_props[prop])}"
                )
        old_required, new_required = (
            set(schema.get("required", [])),
            set(new_schema.get("required", [])),
        )
        for prop in sorted(old_required - new_required):
            if prop in new_props:
                problems.append(f"required field {name}.{prop} became optional")
        for prop in sorted(new_required - old_required):
            problems.append(f"new required field {name}.{prop}")
        if "enum" in schema and _type_signature(schema) != _type_signature(new_schema):
            problems.append(f"changed enum {name}")
    return problems


def write_snapshots(out_dir: Path, services: dict[str, str] = SERVICES) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, spec in services.items():
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(snapshot(load_app(spec)), indent=1, sort_keys=True) + "\n")
        written.append(path)
    return written


def check_snapshots(out_dir: Path, services: dict[str, str] = SERVICES) -> int:
    """0 when every committed snapshot is compatible with the app, 1 otherwise."""
    failed = False
    for name, spec in services.items():
        path = out_dir / f"{name}.json"
        if not path.exists():
            print(f"{name}: no snapshot at {path}; run scripts/openapi_snapshot.py")
            failed = True
            continue
        old = json.loads(path.read_text())
        new = snapshot(load_app(spec))
        problems = breaking_changes(old, new)
        if problems:
            failed = True
            print(f"{name}: BREAKING")
            for p in problems:
                print(f"  {p}")
        elif old != new:
            print(
                f"{name}: additive changes; run scripts/openapi_snapshot.py to refresh the snapshot"
            )
        else:
            print(f"{name}: unchanged")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="compare, do not write; exit 1 on a break")
    ap.add_argument("--out", type=Path, default=DEFAULT_DIR)
    args = ap.parse_args(argv)
    if args.check:
        return check_snapshots(args.out)
    for path in write_snapshots(args.out):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
