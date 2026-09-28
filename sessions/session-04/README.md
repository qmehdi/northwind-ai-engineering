# Session 4: Generative AI, retrieval and evaluation

What you build: Project 3, the policy-grounded response service in `nw/policy`.

```bash
make session04
make index-policy            # chunk data/policies, build artifacts/policy, write its manifest
uv run python -m nw.policy.build_index --check     # exit 1 when the index no longer matches the corpus
uv run python -m nw.llm.prompts                    # every registered prompt with its hash
make eval-policy             # run the golden set, print the report, apply the regression gate
uv run python -m nw.policy.evaluate --retrieval-only   # the free gate: recall and MRR, no model
make serve-policy            # the service on :8003
uv run python -m nw.policy.feedback --to-golden    # wrong and unsafe verdicts as golden candidates
```

Files you edit in this session:

- `nw/policy/redact.py`: `redact`
- `nw/policy/chunking.py`: `split_sections`
- `nw/policy/retrieval.py`: `rrf`, `PolicyIndex.retrieve`
- `nw/policy/answer.py`: `answer`, including `prompt_version` on every Answer
- `nw/policy/evaluate.py`: `retrieval_metrics`, `gate` with its `keys`

Files you read but do not edit: `build_index.py` and `manifest.py` (the manifest and the stale check), `service.py` (cache, feedback, drift, capture), `cache.py`, `feedback.py`, `monitor.py`, `nw/llm/prompts` (the registry), the rest of `evaluate.py`, and `.github/workflows/eval-gate.yml`.

Environment for the service, all optional: `NW_POLICY_CACHE_TTL_S` (0 is off) and `NW_POLICY_CACHE_SIZE`, `NW_POLICY_FEEDBACK`, `NW_POLICY_CAPTURE`, `NW_POLICY_CORPUS`, `NW_POLICY_DRIFT_WINDOW`, `NW_POLICY_DRIFT_MIN`, `NW_POLICY_DRIFT_EVERY`.
