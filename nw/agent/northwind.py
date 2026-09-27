"""The Northwind tools: Projects 1 to 3 and the customer fixtures, as tools.

Two backends behind one registry: `local` calls the models and indexes in
process (Sessions 5 and 6 on a laptop); `http` calls the deployed services
(the Reference stack). Tests use a third: fakes registered with the same
names and schemas, which is the point of a registry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nw.agent.tools import ToolRegistry


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
    account_id: str = Field(
        pattern=r"^NW-\d{5,}$", description="Northwind account id, like NW-10007"
    )


class CheckEntitlement(BaseModel):
    account_id: str = Field(pattern=r"^NW-\d{5,}$")
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


def load_accounts(path: Path) -> dict[str, dict[str, Any]]:
    return {a["account_id"]: a for a in json.loads(path.read_text())}


def build_registry(
    backend: str = "local",
    *,
    accounts_path: Path = Path("data/accounts.json"),
    artifacts: Path = Path("artifacts"),
) -> ToolRegistry:
    reg = ToolRegistry()
    accounts = load_accounts(accounts_path) if accounts_path.exists() else {}

    @reg.tool(
        "lookup_customer",
        "Look up a Northwind customer account: company, plan tier, region, seats, SLA hours, "
        "features.",
    )
    def lookup_customer(args: LookupCustomer) -> dict[str, Any]:
        acct = accounts.get(args.account_id)
        if acct is None:
            raise KeyError(f"no account {args.account_id}")
        return acct

    @reg.tool(
        "check_entitlement",
        "Check whether an account's plan includes a feature. Returns entitled true or false "
        "with the reason.",
    )
    def check_entitlement(args: CheckEntitlement) -> dict[str, Any]:
        return {"entitled": True, "reason": "not implemented"}  # Step 2

    @reg.tool(
        "escalate",
        "Escalate a ticket to a tier with a justification. Irreversible; requires human approval.",
        requires_approval=True,
        idempotent=False,
    )
    def escalate(args: Escalate) -> dict[str, Any]:
        ESCALATION_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        with ESCALATION_QUEUE.open("a") as f:
            f.write(json.dumps(args.model_dump()) + "\n")
        return {"queued": True, **args.model_dump()}

    if backend == "http":
        _register_http(reg)
    elif backend == "local":
        _register_local(reg, artifacts)
    elif backend == "none":
        pass
    else:
        raise ValueError(f"unknown backend {backend}")
    return reg


def _register_local(reg: ToolRegistry, artifacts: Path) -> None:
    """Lazy: each tool loads its model on first call, so building the registry is cheap
    and a missing artifact is an observation the model can read, not a crash at import."""
    state: dict[str, Any] = {}

    @reg.tool(
        "search_policies",
        "Search Northwind's current customer-facing policies. Returns passages with ids, "
        "sections and effective dates.",
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
    )
    def classify_urgency(args: ClassifyUrgency) -> dict[str, Any]:
        if "triage" not in state:
            from nw.triage.model import TriageModel

            state["triage"] = TriageModel.load(artifacts / "triage" / "latest")
        return state["triage"].predict([args.model_dump()])[0].model_dump()

    @reg.tool(
        "classify_semantic", "Tag a ticket with topics and a priority using the semantic engine."
    )
    def classify_semantic(args: ClassifySemantic) -> dict[str, Any]:
        if "semantic" not in state:
            import numpy as np
            from transformers import AutoTokenizer

            from nw.semantic.data import TAGS
            from nw.semantic.export import OnnxEncoder

            d = artifacts / "semantic"
            meta = json.loads((d / "metadata.json").read_text())
            tok = AutoTokenizer.from_pretrained(str(d / "tokenizer"))
            state["semantic"] = (
                OnnxEncoder(d / "model.int8.onnx", tok, meta["max_length"]),
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
    )
    def find_similar_tickets(args: SimilarTickets) -> list[dict[str, Any]]:
        if "index" not in state:
            from nw.semantic.embed import Embedder, TicketIndex

            d = artifacts / "index"
            state["index"] = (
                TicketIndex.load(d),
                Embedder(json.loads((d / "metadata.json").read_text())["embedder"]),
            )
        from nw.triage.features import ticket_text

        index, embedder = state["index"]
        q = embedder.encode([ticket_text(args.subject, args.body)])
        return [
            {"ticket_id": i, "score": round(s, 3), **m} for i, s, m in index.search(q, args.k)[0]
        ]


def _register_http(reg: ToolRegistry) -> None:

    urls = {
        "policy": os.environ.get("NW_POLICY_URL", "http://localhost:8003"),
        "triage": os.environ.get("NW_TRIAGE_URL", "http://localhost:8001"),
        "semantic": os.environ.get("NW_SEMANTIC_URL", "http://localhost:8002"),
    }
    from nw.auth import service_client

    client = service_client(timeout=30)

    @reg.tool(
        "search_policies",
        "Search Northwind's current customer-facing policies. Returns an answer with "
        "citations, or a refusal.",
    )
    async def search_policies(args: SearchPolicies) -> dict[str, Any]:
        r = await client.post(f"{urls['policy']}/ask", json={"question": args.query, "k": args.k})
        r.raise_for_status()
        return r.json()

    @reg.tool(
        "classify_urgency", "Score a ticket's priority P0 to P3 with the calibrated triage model."
    )
    async def classify_urgency(args: ClassifyUrgency) -> dict[str, Any]:
        r = await client.post(f"{urls['triage']}/triage", json=args.model_dump())
        r.raise_for_status()
        return r.json()

    @reg.tool(
        "classify_semantic",
        "Tag a ticket with topics and a priority, and list similar past tickets.",
    )
    async def classify_semantic(args: ClassifySemantic) -> dict[str, Any]:
        r = await client.post(f"{urls['semantic']}/classify", json={**args.model_dump(), "k": 3})
        r.raise_for_status()
        return r.json()
