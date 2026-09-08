# Session 4: Generative AI, retrieval and evaluation

What you build: Project 3, the policy-grounded response service in `nw/policy`.

```bash
make session04
make index-policy            # chunk data/policies and build artifacts/policy
make eval-policy             # run the golden set, print the report, apply the regression gate
make serve-policy            # the service on :8003
```

Files you edit in this session:

- `nw/policy/redact.py`: `redact`
- `nw/policy/chunking.py`: `split_sections`
- `nw/policy/retrieval.py`: `rrf`, `PolicyIndex.retrieve`
- `nw/policy/answer.py`: `answer`
- `nw/policy/evaluate.py`: `retrieval_metrics`, `gate`

Files you read but do not edit: `build_index.py`, `service.py`, the rest of `evaluate.py`.
