# Generative AI: retrieval and evaluation

What you build: Project 3, the policy-grounded response service in `nw/policy`, measured by a harness with a provenance-checked gate, with its prompt in the platform's prompt store and its chunks in the platform's vector store.

```bash
make session04                  # the acceptance tests (96 on the solution)
make pii-eval                   # redaction recall and precision on data/pii/messages.jsonl (DETECTOR=none|heuristic|presidio)
make index-policy               # chunk data/policies, build artifacts/policy, write its manifest
make check-index                # exit 1 when the index no longer matches the corpus
make prompts                    # every registered prompt with its hash
make eval-policy-free           # the free gate: retrieval only, no model, the held-out gate split
make eval-policy-baseline       # the Workhorse without the Judge on your track; writes its baseline when none exists (JUDGE=1 for the judged one)
make eval-policy-baseline REASON="..."  # rebase a baseline the gate refuses after a deliberate change; the reason and the old provenance are kept in the file
make calibrate-judge            # the Judge against 102 labelled answers: agreement, kappa, error rates with intervals, mean bias
make eval-policy                # the judged harness and the gate against your track's judged baseline
make eval-policy-baseline-free  # rewrite the retrieval-only baseline after a corpus or golden-set change
make serve-policy               # the service on :8003; NW_RETRIEVER=inprocess|knowledge-base|rag-engine|ai-search|qdrant
make feedback-policy            # wrong and unsafe verdicts as golden-set candidates
```

The harness directly: `uv run python -m nw.policy.evaluate` with `--retrieval-only` or `--no-judge`, `--split gate|dev|all`, `--baseline auto|none|<path>`, `--write-baseline`, `--rebase "<reason>"`, `--allow-model-change`, and the experiment flags `--k`, `--min-score`, `--no-hybrid`, `--no-rerank`.

Baselines live in `data/golden/baselines/` per mode and track, each with its provenance (mode, track, split, model ids, prompt versions, corpus and golden hashes); `data/golden/baseline.json` is the legacy Claude-era run, kept for the record and refused by the gate. The golden set `data/golden/policy_qa.jsonl` has 111 cases, 66 in the held-out `gate` split and 45 in `dev`.

On the platform (`nw/platform/base.py`): `platform.prompts.register`, `set_stage` for the prompt; `platform.vectors.upsert(tenant, "policies", ...)`, `search`, `count` for the index. Every model call goes through the model gateway with `NW_GATEWAY_URL` and `NW_GATEWAY_KEY` (API Management with your subscription key on Azure).

Files you edit: `nw/policy/redact.py` (`redact`), `nw/policy/chunking.py` (`split_sections`), `nw/policy/retrieval.py` (`rrf`, `PolicyIndex.retrieve`), `nw/policy/answer.py` (`answer`), `nw/policy/evaluate.py` (`retrieval_metrics`, `gate`), `nw/policy/calibrate.py` (`agreement`).

Files you read: `build_index.py`, `manifest.py`, `service.py`, `cache.py`, `feedback.py`, `monitor.py`, `nw/evalstats.py`, `nw/llm/prompts`, `nw/platform/retrievers.py`, `.github/workflows/eval-gate.yml`, `data/golden/baselines/README.md`.

Service environment, all optional: `NW_API_KEY` (a JSON map of key ids to secrets), `NW_POLICY_AUDIENCES` (key ids to `internal`; every other caller is a customer), `NW_RETRIEVER`, `NW_POLICY_MIN_SCORE`, `NW_POLICY_CACHE_TTL_S` (0 is off), `NW_POLICY_CACHE_SIZE`, `NW_POLICY_FEEDBACK`, `NW_POLICY_FEEDBACK_PER_ANSWER` (default 3), `NW_POLICY_CAPTURE`, `NW_POLICY_CORPUS`, `NW_POLICY_DRIFT_WINDOW`, `NW_POLICY_DRIFT_MIN` (default 200), `NW_POLICY_DRIFT_EVERY`, `NW_OPS_STORE` (feedback in the platform's object storage), `NW_REDACT_DETECTOR`; screening with `NW_GUARDRAIL_ID` (Bedrock Guardrails), `NW_MODEL_ARMOR_TEMPLATE` (Model Armor) or `NW_AZURE_CONTENT_SAFETY_ENDPOINT` (Prompt Shields), Llama Guard in the gateway on the Local track.
