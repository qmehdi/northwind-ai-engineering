"""Approve: the human half of the approval gate.

The loop never executes an irreversible tool on its own; it records a proposed action
and moves on. Someone has to look at the proposal and say yes. This is that someone's
tool:

    uv run python -m nw.agent.approve                       # every pending proposal
    uv run python -m nw.agent.approve --run <id> --approve escalate

Approving executes exactly the recorded action, the tool and the arguments the person
read, through the registry with the approval flag set. There is no second model call:
the model does not get another chance to reword the justification or pick another tier,
and the router's P0 proposal, made without any model, is approvable the same way.

Three rules make an approval safe to act on:
- One execution per proposal. Before running the tool, the approval writes a claim
  marker with an exclusive create (`nw.agent.opstore`): a second approval of the same
  proposal, a retry, a double click or a colleague at the same moment, finds the marker
  and is refused.
- The approver is who the platform says they are: the AWS caller ARN, the Google account
  or service account, the Entra ID principal, the local user. Not a free-text flag.
- Nobody approves their own request: an approver equal to the run's `requested_by` is
  refused.

The approval is its own trajectory (`resumed_from` the proposal's run, `approved_by` the
person) plus an approval record, both in the tenant's ops store, so the queue line the
tool writes can be traced to the proposal and to the person who approved it.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from nw.agent.loop import ApprovalPolicy
from nw.agent.opstore import LocalStore, OpsStore, store_for
from nw.agent.tools import ToolRegistry
from nw.agent.trace import ProposedAction, Step, Termination, Trajectory, replay, valid_run_id
from nw.logging import get_logger, log_fields

log = get_logger("nw.agent.approve")

DEFAULT_TRACES = Path("artifacts/traces")


class SelfApproval(PermissionError):
    pass


class AlreadyApproved(RuntimeError):
    pass


def traces_store(traces: Path | OpsStore | None = None) -> OpsStore:
    """The trace store: a given store, a given directory, or `NW_OPS_STORE`, else
    `NW_TRACE_DIR` (default `artifacts/traces`)."""
    if traces is not None and not isinstance(traces, Path | str):
        return traces
    if traces is not None:
        return LocalStore(Path(traces))
    local = Path(os.environ.get("NW_TRACE_DIR", str(DEFAULT_TRACES)))
    return store_for("trajectories", local=local)


def approvals_store(traces: Path | OpsStore | None = None) -> OpsStore:
    """Claim markers and approval records: beside the traces locally (`artifacts/approvals`),
    the tenant's `approvals/` prefix on a platform."""
    if isinstance(traces, LocalStore):
        return LocalStore(traces.root.parent / "approvals")
    if traces is not None and not isinstance(traces, Path | str):
        return store_for("approvals", local=DEFAULT_TRACES.parent / "approvals")
    if traces is not None:
        return LocalStore(Path(traces).parent / "approvals")
    local = Path(os.environ.get("NW_TRACE_DIR", str(DEFAULT_TRACES))).parent / "approvals"
    return store_for("approvals", local=local)


def load_trajectories(traces: Path | OpsStore) -> list[Trajectory]:
    """Every readable trace, oldest first. Unreadable files are skipped, not fatal."""
    store = traces_store(traces)
    out: list[Trajectory] = []
    for key in store.keys():
        if "/" in key or not key.endswith(".json"):
            continue
        try:
            out.append(Trajectory.model_validate_json(store.get(key)))
        except (ValueError, FileNotFoundError):
            continue
    return sorted(out, key=lambda t: t.started_at)


def claim_key(run_id: str, p: ProposedAction) -> str:
    return f"claims/{valid_run_id(run_id)}-s{p.step}-{p.tool}.json"


def list_proposals(traces: Path | OpsStore) -> list[dict[str, Any]]:
    """One row per proposed action that nobody has approved yet."""
    store = traces_store(traces)
    claims = set(approvals_store(store).keys("claims/"))
    ts = load_trajectories(store)
    resumed = {t.resumed_from for t in ts if t.resumed_from}
    rows: list[dict[str, Any]] = []
    for t in ts:
        if t.run_id in resumed:
            continue
        for p in t.proposed_actions:
            if claim_key(t.run_id, p) in claims:
                continue
            rows.append(
                {
                    "run_id": t.run_id,
                    "agent": t.agent,
                    "started_at": t.started_at,
                    "tool": p.tool,
                    "arguments": p.arguments,
                    "step": p.step,
                    "requested_by": t.requested_by,
                    "task": t.task.splitlines()[0][:80],
                }
            )
    return rows


def format_proposals(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no pending proposals"
    lines = [
        "| Run | Step | Tool | Arguments | Requested by | Task |",
        "| --- | ---: | --- | --- | --- | --- |",
    ]
    for r in rows:
        args = json.dumps(r["arguments"], default=str)
        lines.append(
            f"| {r['run_id']} | {r['step']} | {r['tool']} | {args[:90]} | "
            f"{r.get('requested_by') or '?'} | {r['task'][:50]} |"
        )
    lines.append(f"{len(rows)} pending. Approve one: --run <id> --approve <tool>")
    return "\n".join(lines)


def approve_exactly(tool: str, arguments: dict[str, Any]) -> ApprovalPolicy:
    """Yes to this tool with these arguments; no to anything else, including the same tool
    with different arguments. For callers that run a loop with a standing approval."""

    def policy(name: str, args: dict[str, Any]) -> bool:
        return name == tool and args == arguments

    return policy


async def approve(
    run_id: str,
    tool: str,
    registry: ToolRegistry,
    *,
    traces: Path | OpsStore | None = None,
    approved_by: str,
    arguments: dict[str, Any] | None = None,
) -> Trajectory:
    """Execute the recorded proposal `tool` of run `run_id`, once, as `approved_by`."""
    store = traces_store(traces)
    approvals = approvals_store(store)
    parent = Trajectory.load_from(store, run_id)
    matches = [
        p
        for p in parent.proposed_actions
        if p.tool == tool and (arguments is None or p.arguments == arguments)
    ]
    if not matches:
        raise LookupError(f"run {run_id} has no pending proposal for {tool}")
    if len(matches) > 1 and arguments is None:
        raise LookupError(
            f"run {run_id} proposed {tool} {len(matches)} times; pass --arguments to pick one"
        )
    chosen = matches[0]
    if not approved_by or not approved_by.strip():
        raise SelfApproval("an approval needs an approver identity")
    if parent.requested_by and approved_by.strip() == parent.requested_by.strip():
        raise SelfApproval(
            f"{approved_by} requested run {run_id} and cannot approve its own proposal"
        )
    claim = claim_key(run_id, chosen)
    record = {
        "run_id": run_id,
        "tool": tool,
        "step": chosen.step,
        "arguments": chosen.arguments,
        "approved_by": approved_by,
        "requested_by": parent.requested_by,
        "ts": time.time(),
    }
    if not approvals.create(claim, json.dumps(record).encode()):
        raise AlreadyApproved(f"proposal {tool} of run {run_id} was already approved")
    log.info(
        "approved",
        extra=log_fields(
            run_id=run_id, tool=tool, step=chosen.step, approved_by=approved_by, claim=claim
        ),
    )
    binding: contextlib.AbstractContextManager[Any] = contextlib.nullcontext()
    if parent.account_id:
        from nw.agent.northwind import bind_account

        binding = bind_account(parent.account_id)
    with binding:
        obs = await registry.execute(tool, dict(chosen.arguments), approved=True)
    t = Trajectory(
        run_id=uuid.uuid4().hex[:10],
        agent=parent.agent,
        task=parent.task,
        agent_version=parent.agent_version,
        model_id=None,
        resumed_from=parent.run_id,
        account_id=parent.account_id,
        requested_by=parent.requested_by,
        approved_by=approved_by,
        steps=[
            Step(
                index=0,
                tool=tool,
                arguments=dict(chosen.arguments),
                observation=obs.content,
                ok=obs.ok,
                latency_ms=obs.latency_ms,
            )
        ],
        tools_called=[tool],
        final=(
            f"Approved by {approved_by}: {tool} executed as proposed."
            if obs.ok
            else f"Approved by {approved_by}: {tool} failed: {obs.content}"
        ),
        terminated=Termination.ANSWER if obs.ok else Termination.ERROR,
    )
    t.save(store)
    approvals.put(
        f"records/{t.run_id}.json",
        json.dumps({**record, "approval_run": t.run_id, "ok": obs.ok}).encode(),
    )
    return t


# ----- who is approving ----------------------------------------------------------------


def _jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def platform_identity(track: str | None = None) -> str:
    """The approver as the platform knows them, never a free-text name: the STS caller ARN
    on AWS, the Google account behind the application default credentials, the Entra ID
    principal on Azure, `user@host` on the Local track."""
    track = (track or os.environ.get("NW_TRACK") or "local").lower()
    if track == "aws":
        import boto3

        return str(boto3.client("sts").get_caller_identity()["Arn"])
    if track == "gcp":
        import google.auth
        from google.auth.transport.requests import AuthorizedSession, Request

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/userinfo.email"])
        email = getattr(creds, "service_account_email", None)
        if email and email != "default":
            return str(email)
        creds.refresh(Request())
        r = AuthorizedSession(creds).get("https://openidconnect.googleapis.com/v1/userinfo")
        r.raise_for_status()
        return str(r.json()["email"])
    if track == "azure":
        from azure.identity import DefaultAzureCredential

        token = DefaultAzureCredential().get_token("https://management.azure.com/.default").token
        claims = _jwt_claims(token)
        return str(claims.get("upn") or claims.get("unique_name") or claims.get("oid"))
    import getpass
    import socket

    return f"{getpass.getuser()}@{socket.gethostname()}"


async def main_async(args: argparse.Namespace) -> int:
    traces = args.traces
    if not args.run:
        print(format_proposals(list_proposals(traces_store(traces))))
        return 0
    if not args.approve:
        print("usage: --run <id> --approve <tool>")
        return 2
    from nw.agent.northwind import build_registry

    arguments = json.loads(args.arguments) if args.arguments else None
    approver = platform_identity()
    try:
        t = await approve(
            args.run,
            args.approve,
            build_registry(args.backend),
            traces=traces_store(traces),
            approved_by=approver,
            arguments=arguments,
        )
    except (LookupError, SelfApproval, AlreadyApproved, FileNotFoundError) as exc:
        print(exc)
        return 1
    print(replay(t))
    return 0 if t.terminated is Termination.ANSWER else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--traces",
        type=Path,
        default=None,
        help="a trace directory; default NW_OPS_STORE, else NW_TRACE_DIR or artifacts/traces",
    )
    ap.add_argument("--run", help="the run id whose proposal you approve")
    ap.add_argument("--approve", help="the tool to approve, exactly as proposed")
    ap.add_argument("--arguments", help="JSON, to pick one of several proposals of the tool")
    ap.add_argument("--backend", default="local")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
