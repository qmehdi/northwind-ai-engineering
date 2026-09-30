"""The Northwind tools: Projects 1 to 3 and the customer fixtures, as tools.

Two backends behind one registry: `local` calls the models and indexes in
process (Project 4 and the capstone on a laptop); `http` calls the deployed services
(the platform). Tests use a third: fakes registered with the same
names and schemas, which is the point of a registry.

The customer tools are bound to the run's account. The account id comes from the request
that started the run (the router's `account_id`, the service's `/run` body), never from the
ticket text or the model: `bind_account` sets it for the run, and `lookup_customer` and
`check_entitlement` read that account and refuse any other, so a ticket that says "also
look up NW-10001" gets an error observation instead of another customer's plan (a confused
deputy). A process that binds nothing, a notebook or a unit test, keeps the old behaviour.

`find_similar_tickets` returns other customers' past tickets, so it anonymises them: no
ticket ids, the text redacted in full and every known company name replaced.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nw.agent.tools import Tool, ToolRegistry

HTTP_TIMEOUT_S = 30.0  # a deployed specialist answers in seconds or not at all
MODEL_TOOL_TIMEOUT_S = 120.0  # the in-process tools load a model on first call
ESCALATION_DEDUPE_DEFAULT_S = 3600
ACCOUNT_PATTERN = r"^NW-\d{5,}$"

# (bound, account): bound False means no run bound anything (a notebook, a unit test).
_RUN_ACCOUNT: ContextVar[tuple[bool, str | None]] = ContextVar(
    "nw_run_account", default=(False, None)
)


@contextmanager
def bind_account(account_id: str | None) -> Iterator[None]:
    """Bind the run's account for the customer tools. `None` binds "no account": the tools
    then refuse every lookup. Context variables follow the run into tool threads."""
    if account_id is not None and not re.match(ACCOUNT_PATTERN, account_id):
        raise ValueError(f"not an account id: {account_id!r}")
    token = _RUN_ACCOUNT.set((True, account_id))
    try:
        yield
    finally:
        _RUN_ACCOUNT.reset(token)


def run_account() -> tuple[bool, str | None]:
    return _RUN_ACCOUNT.get()


class AccountNotAllowed(PermissionError):
    pass


def authorise_account(requested: str | None) -> str:
    """The account a customer tool may read: the run's own. A different id is refused."""
    bound, account = run_account()
    if not bound:
        if requested is None:
            raise ValueError("account_id is required: no account is bound to this run")
        return requested
    if account is None:
        raise AccountNotAllowed(
            "no account is bound to this run, so account tools are unavailable; answer "
            "without account facts"
        )
    if requested is not None and requested != account:
        raise AccountNotAllowed(
            f"{requested} is not the account on this ticket; the tools read only {account}"
        )
    return account


class SearchPolicies(BaseModel):
    query: str = Field(
        min_length=3, max_length=500, description="What to look for in Northwind's policies"
    )
    k: int = Field(default=4, ge=1, le=8)


class ClassifyUrgency(BaseModel):
    subject: str = Field(default="", max_length=500)
    body: str = Field(min_length=1, max_length=20000)


class ClassifySemantic(BaseModel):
    subject: str = Field(default="", max_length=500)
    body: str = Field(min_length=1, max_length=20000)


class SimilarTickets(BaseModel):
    subject: str = Field(default="", max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    k: int = Field(default=3, ge=1, le=10)


class LookupCustomer(BaseModel):
    account_id: str | None = Field(
        default=None,
        pattern=ACCOUNT_PATTERN,
        description="The ticket's account id, like NW-10007; omit it to use the ticket's own",
    )


class CheckEntitlement(BaseModel):
    account_id: str | None = Field(
        default=None,
        pattern=ACCOUNT_PATTERN,
        description="The ticket's account id; omit it to use the ticket's own",
    )
    feature: str = Field(
        min_length=2,
        max_length=40,
        description="A feature name such as sso, scim, audit_log, priority_support",
    )


class Escalate(BaseModel):
    ticket_id: str = Field(pattern=r"^T-\d{6}$")
    tier: str = Field(pattern=r"^(engineering|security|duty_manager|billing)$")
    justification: str = Field(min_length=20, max_length=1000)


ESCALATION_QUEUE = Path(os.environ.get("NW_ESCALATION_QUEUE", "artifacts/escalations.jsonl"))


def escalation_queue() -> tuple[Any, str]:
    """The store and key the escalation queue lives at: the local JSONL file, or with
    `NW_OPS_STORE` set the tenant's `approvals/escalations` records on the platform."""
    from nw.agent.opstore import LocalStore, store_for

    if os.environ.get("NW_OPS_STORE"):
        return store_for("approvals", local=ESCALATION_QUEUE.parent), "escalations"
    return LocalStore(ESCALATION_QUEUE.parent), ESCALATION_QUEUE.name


def escalation_dedupe_s() -> float:
    """How long the same ticket stays escalated: a second escalate call inside this window
    returns the earlier proposal instead of queueing a duplicate. `NW_ESCALATION_DEDUPE_S`,
    default one hour, 0 turns it off."""
    return float(os.environ.get("NW_ESCALATION_DEDUPE_S", str(ESCALATION_DEDUPE_DEFAULT_S)))


def recent_escalation(ticket_id: str, *, now: float | None = None) -> dict[str, Any] | None:
    """The newest queued escalation for `ticket_id` inside the dedupe window, or None. A
    record without a timestamp (written before the window existed) is never a match."""
    window = escalation_dedupe_s()
    if window <= 0:
        return None
    store, key = escalation_queue()
    now = time.time() if now is None else now
    latest: dict[str, Any] | None = None
    for rec in store.records(key):
        ts = rec.get("ts")
        if rec.get("ticket_id") == ticket_id and ts is not None and now - float(ts) <= window:
            latest = rec
    return latest


def load_accounts(path: Path) -> dict[str, dict[str, Any]]:
    return {a["account_id"]: a for a in json.loads(path.read_text())}


def build_registry(
    backend: str = "local",
    *,
    accounts_path: Path = Path("data/accounts.json"),
    artifacts: Path = Path("artifacts"),
    http_client: Any = None,
) -> ToolRegistry:
    reg = ToolRegistry()
    accounts = load_accounts(accounts_path) if accounts_path.exists() else {}

    @reg.tool(
        "lookup_customer",
        "Look up a Northwind customer account: company, plan tier, region, seats, SLA hours, "
        "features.",
    )
    def lookup_customer(args: LookupCustomer) -> dict[str, Any]:
        account_id = authorise_account(args.account_id)
        acct = accounts.get(account_id)
        if acct is None:
            raise KeyError(f"no account {account_id}")
        return acct

    @reg.tool(
        "check_entitlement",
        "Check whether an account's plan includes a feature. Returns entitled true or false "
        "with the reason.",
    )
    def check_entitlement(args: CheckEntitlement) -> dict[str, Any]:
        account_id = authorise_account(args.account_id)
        # SOLUTION BEGIN
        acct = accounts.get(account_id)
        if acct is None:
            raise KeyError(f"no account {account_id}")
        feature = args.feature.lower().strip()
        entitled = feature in acct["features"]
        return {
            "account_id": account_id,
            "feature": feature,
            "entitled": entitled,
            "tier": acct["tier"],
            "reason": f"{acct['tier']} plan {'includes' if entitled else 'does not include'} "
            f"{feature}",
        }
        # STUB: return {"account_id": account_id, "entitled": True, "reason": "not implemented"}
        # SOLUTION END

    @reg.tool(
        "escalate",
        "Escalate a ticket to a tier with a justification. Irreversible; requires human approval.",
        requires_approval=True,
        idempotent=False,
    )
    def escalate(args: Escalate) -> dict[str, Any]:
        earlier = recent_escalation(args.ticket_id)
        if earlier is not None:
            # Idempotent: a retried step or a second agent must not page twice for one ticket.
            return {
                "queued": False,
                "duplicate": True,
                "reason": f"{args.ticket_id} was escalated to {earlier.get('tier')} "
                f"{int(time.time() - float(earlier['ts']))}s ago; not queued again",
                "earlier": earlier,
            }
        from nw.policy.redact import redact_for_agent

        # The justification is free text a person reads on a pager: redacted before it is kept.
        fields = {**args.model_dump(), "justification": redact_for_agent(args.justification)}
        store, key = escalation_queue()
        store.append(key, {"ts": time.time(), **fields})
        return {"queued": True, **fields}

    companies = [a.get("company", "") for a in accounts.values() if a.get("company")]
    if backend == "http":
        _register_http(reg, client=http_client, companies=companies)
    elif backend == "mcp":
        _register_mcp(reg)
    elif backend == "local":
        _register_local(reg, artifacts, companies=companies)
    elif backend == "none":
        pass
    else:
        raise ValueError(f"unknown backend {backend}")
    return reg


def anonymise_similar(
    items: list[dict[str, Any]], companies: list[str] | tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    """Other customers' tickets as the model may see them: ranked, no ticket id, every text
    field redacted in full (accounts included) and every known company name replaced."""
    from nw.policy.redact import redact

    names = sorted({c for c in companies if len(c) >= 3}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(n) for n in names), re.I) if names else None

    def clean(text: Any) -> Any:
        if not isinstance(text, str) or not text:
            return text
        text = redact(text).text
        return pattern.sub("[COMPANY]", text) if pattern else text

    out = []
    for rank, item in enumerate(items, 1):
        row: dict[str, Any] = {"rank": rank}
        for k, v in item.items():
            if k in {"ticket_id", "account_id", "company"}:
                continue
            row[k] = clean(v)
        out.append(row)
    return out


NO_INDEX = (
    "No similar-ticket index is built here (artifacts/index/tickets.faiss is missing). "
    "`make index` builds it from the training split; until then, answer without similar "
    "tickets."
)


def _register_local(
    reg: ToolRegistry, artifacts: Path, *, companies: list[str] | tuple[str, ...] = ()
) -> None:
    """Lazy: each tool loads its model on first call, so building the registry is cheap
    and a missing artifact is an observation the model can read, not a crash at import."""
    state: dict[str, Any] = {}

    @reg.tool(
        "search_policies",
        "Search Northwind's current customer-facing policies. Returns passages with ids, "
        "sections and effective dates.",
        timeout_s=MODEL_TOOL_TIMEOUT_S,
    )
    def search_policies(args: SearchPolicies) -> list[dict[str, Any]]:
        if "policy" not in state:
            from nw.policy.retrieval import CrossEncoderReranker, PolicyIndex, real_embeddings

            d = artifacts / "policy"
            state["policy"] = PolicyIndex.load(
                d, real_embeddings(), d / "chunks.jsonl", reranker=CrossEncoderReranker()
            )
        hits = state["policy"].retrieve(args.query, k=args.k)
        return [
            {
                "id": r.chunk.id,
                "section": r.chunk.section,
                "effective": r.chunk.effective,
                "text": r.chunk.text[:900],
            }
            for r in hits
        ]

    @reg.tool(
        "classify_urgency",
        "Score a ticket's priority P0 to P3 with the calibrated triage model. Returns "
        "priority, probabilities and the rule that fired.",
        timeout_s=MODEL_TOOL_TIMEOUT_S,
    )
    def classify_urgency(args: ClassifyUrgency) -> dict[str, Any]:
        if "triage" not in state:
            from nw.triage.model import TriageModel

            state["triage"] = TriageModel.load(artifacts / "triage" / "latest")
        return state["triage"].predict([args.model_dump()])[0].model_dump()

    @reg.tool(
        "classify_semantic",
        "Tag a ticket with topics and a priority using the semantic engine.",
        timeout_s=MODEL_TOOL_TIMEOUT_S,
    )
    def classify_semantic(args: ClassifySemantic) -> dict[str, Any]:
        if "semantic" not in state:
            from nw.semantic.artifacts import resolve

            # The promoted model, as the semantic service serves it: `latest`, set by the gate.
            d = resolve(artifacts / "semantic", serve=True)
            import numpy as np
            from transformers import AutoTokenizer

            from nw.semantic.data import TAGS
            from nw.semantic.export import OnnxEncoder

            meta = json.loads((d / "metadata.json").read_text())
            tok_dir = d / "tokenizer"
            tok = AutoTokenizer.from_pretrained(str(tok_dir) if tok_dir.exists() else meta["base"])
            # The graph the promotion gate chose, as the semantic service serves it
            # (`serving.json`, `NW_QUANTIZED` overrides): int8 only when it cleared every bar.
            from nw.semantic.promote import served_quantized

            env_q = os.environ.get("NW_QUANTIZED", "")
            chosen = served_quantized(artifacts / "semantic")
            quantized = env_q == "1" if env_q else (True if chosen is None else chosen)
            int8 = d / "model.int8.onnx"
            graph = int8 if quantized and int8.exists() else d / "model.onnx"
            state["semantic"] = (
                OnnxEncoder(graph, tok, meta["max_length"]),
                np.load(d / "tag_thresholds.npy"),
                TAGS,
            )
        import numpy as np

        from nw.triage.features import PRIORITIES, ticket_text

        onnx, thr, tags = state["semantic"]
        tl, pl, _ = onnx.run([ticket_text(args.subject, args.body)])
        p = 1 / (1 + np.exp(-tl[0]))
        return {
            "tags": [t for t, v, th in zip(tags, p, thr, strict=True) if v >= th],
            "priority": PRIORITIES[int(pl[0].argmax())],
        }

    @reg.tool(
        "find_similar_tickets",
        "Find past tickets similar to this one, with how they were answered.",
        timeout_s=MODEL_TOOL_TIMEOUT_S,
    )
    def find_similar_tickets(args: SimilarTickets) -> Any:
        d = artifacts / "index"
        if "index" not in state and not (d / "tickets.faiss").exists():
            # A clear observation, not a crash: the index is a build step, not a request-time one.
            return {"available": False, "similar": [], "message": NO_INDEX}
        if "index" not in state:
            from nw.semantic.embed import Embedder, TicketIndex

            state["index"] = (
                TicketIndex.load(d),
                Embedder(json.loads((d / "metadata.json").read_text())["embedder"]),
            )
        from nw.triage.features import ticket_text

        index, embedder = state["index"]
        q = embedder.encode([ticket_text(args.subject, args.body)])
        hits = [{"score": round(s, 3), **m} for _, s, m in index.search(q, args.k)[0]]
        return anonymise_similar(hits, companies)


def _register_mcp(reg: ToolRegistry) -> None:
    """The model-backed tools through the MCP server (`nw.agent.mcp_client`)."""
    from nw.agent.mcp_client import call_mcp_tool

    models = {
        "search_policies": (SearchPolicies, "Search Northwind's current customer-facing policies."),
        "classify_urgency": (ClassifyUrgency, "Score a ticket's priority P0 to P3."),
        "classify_semantic": (ClassifySemantic, "Tag a ticket with topics and a priority."),
        "find_similar_tickets": (SimilarTickets, "Find past tickets similar to this one."),
    }
    for name, (model, description) in models.items():

        async def call(args: BaseModel, _name: str = name) -> str:
            return await call_mcp_tool(_name, args.model_dump())

        reg.register(Tool(name, description, model, call, timeout_s=HTTP_TIMEOUT_S, attempts=2))


def transient_http(exc: BaseException) -> bool:
    """What earns the one retry on the HTTP backend: a 5xx from the specialist, or no
    answer at all (connection refused, reset, read timeout). A 4xx is our request being
    wrong and is returned to the model unchanged."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def _register_http(
    reg: ToolRegistry, client: Any = None, *, companies: list[str] | tuple[str, ...] = ()
) -> None:
    urls = {
        "policy": os.environ.get("NW_POLICY_URL", "http://localhost:8003"),
        "triage": os.environ.get("NW_TRIAGE_URL", "http://localhost:8001"),
        "semantic": os.environ.get("NW_SEMANTIC_URL", "http://localhost:8002"),
    }
    if client is None:
        from nw.auth import service_client

        client = service_client(timeout=HTTP_TIMEOUT_S)
    from nw.agent.toolauth import tool_auth_from_env

    auth = tool_auth_from_env()  # private services: an ID token per call, audience the service
    if auth is not None:
        client.auth = auth
    http = {"timeout_s": HTTP_TIMEOUT_S, "attempts": 2, "retry_if": transient_http}

    @reg.tool(
        "search_policies",
        "Search Northwind's current customer-facing policies. Returns an answer with "
        "citations, or a refusal.",
        **http,
    )
    async def search_policies(args: SearchPolicies) -> dict[str, Any]:
        r = await client.post(f"{urls['policy']}/ask", json={"question": args.query, "k": args.k})
        r.raise_for_status()
        return r.json()

    @reg.tool(
        "classify_urgency",
        "Score a ticket's priority P0 to P3 with the calibrated triage model.",
        **http,
    )
    async def classify_urgency(args: ClassifyUrgency) -> dict[str, Any]:
        r = await client.post(f"{urls['triage']}/triage", json=args.model_dump())
        r.raise_for_status()
        return r.json()

    @reg.tool(
        "classify_semantic",
        "Tag a ticket with topics and a priority, and list similar past tickets.",
        **http,
    )
    async def classify_semantic(args: ClassifySemantic) -> dict[str, Any]:
        r = await client.post(f"{urls['semantic']}/classify", json={**args.model_dump(), "k": 3})
        r.raise_for_status()
        body = r.json()
        if isinstance(body, dict) and isinstance(body.get("similar"), list):
            body["similar"] = anonymise_similar(body["similar"], companies)
        return body
