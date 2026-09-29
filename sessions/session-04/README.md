# Generative AI: retrieval and evaluation

What you build: Project 3, the policy-grounded response service in `nw/policy`, with its prompt in the platform's prompt store and its chunks in the platform's vector store.

```bash
make session04
make index-policy            # chunk data/policies, build artifacts/policy, write its manifest
make check-index             # exit 1 when the index no longer matches the corpus
make prompts                 # every registered prompt with its hash
make eval-policy             # the golden set, the report, the regression gate (the Judge through the gateway)
make eval-policy-free        # the free gate: recall and MRR, no model
make calibrate-judge         # judge agreement with human labels
make serve-policy            # the service on :8003; NW_RETRIEVER=inprocess|knowledge-base|rag-engine|qdrant
make feedback-policy         # wrong and unsafe verdicts as golden candidates
```

On the platform (`nw/platform/base.py`): `platform.prompts.register`, `set_stage` for the prompt; `platform.vectors.upsert(tenant, "policies", ...)`, `search`, `count` for the index. Every model call goes through the model gateway with `NW_GATEWAY_URL` and `NW_GATEWAY_KEY`.

Files you edit: `nw/policy/redact.py` (`redact`), `nw/policy/chunking.py` (`split_sections`), `nw/policy/retrieval.py` (`rrf`, `PolicyIndex.retrieve`), `nw/policy/answer.py` (`answer`), `nw/policy/evaluate.py` (`retrieval_metrics`, `gate`), `nw/policy/calibrate.py` (`agreement`).

Files you read: `build_index.py`, `manifest.py`, `service.py`, `cache.py`, `feedback.py`, `monitor.py`, `nw/llm/prompts`, `.github/workflows/eval-gate.yml`.

Service environment, all optional: `NW_RETRIEVER`, `NW_POLICY_CACHE_TTL_S` (0 is off), `NW_POLICY_CACHE_SIZE`, `NW_POLICY_FEEDBACK`, `NW_POLICY_CAPTURE`, `NW_POLICY_CORPUS`, `NW_POLICY_DRIFT_WINDOW`, `NW_POLICY_DRIFT_MIN`, `NW_POLICY_DRIFT_EVERY`; screening with `NW_GUARDRAIL_ID` (Bedrock Guardrails) or `NW_MODEL_ARMOR_TEMPLATE` (Model Armor), Llama Guard in the gateway on the Local track.
