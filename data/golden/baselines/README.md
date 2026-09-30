# Baselines

A regression gate compares a run with a baseline. A baseline is only valid for what it was measured with, so every file here carries its provenance and the gates refuse a comparison when it does not match: the mode, the track, the model ids, the prompt versions, the corpus and golden set hashes, the split, the case files and the repeats.

| File | What | How it is written | Written from |
| --- | --- | --- | --- |
| `policy-retrieval_only.json` | Project 3 retrieval on the gate split, no model | `make eval-policy-baseline-free` | committed 2026-09-30 from a local run: embedder `all-MiniLM-L6-v2`, reranker `ms-marco-MiniLM-L-6-v2`, 66 gate cases |
| `policy-<track>-no_judge.json` | Project 3 generation without the Judge, per track | `make eval-policy-baseline` on the track, or `eval-gate` dispatch with `write_baseline=true` | not yet: needs a live run per track |
| `policy-<track>-judged.json` | the same with the faithfulness Judge | `make eval-policy-baseline JUDGE=1` after `make calibrate-judge` | not yet |
| `agent-offline.json` | Project 4 with the scripted model, what CI gates on | `make agent-baseline-offline` | committed 2026-09-30, adversarial and benign cases |
| `agent-<track>.json` | Project 4 on a track, pass^3 | `make agent-baseline-live` after `make agent-calibrate-judge`, or `agent-gate` dispatch with `write_baseline=true` | not yet: needs a live run per track |

Tracks are `aws`, `gcp`, `azure` and `local` (the Local track's Workhorse is gpt-oss-20b, not the clouds' 120b, so its numbers are its own).

A baseline is written when none exists or when the run passed the gate against the current one; a failed run never replaces it. A person reviews and commits the file: the CI jobs only upload it as an artifact.

After a change to `data/golden/policy_qa.jsonl`, the corpus, `data/adversarial/tickets.jsonl` or `data/golden/agent_cases.jsonl`, the offline baselines are rewritten with the two free targets above and the live ones marked stale by the gate itself (it names the hash that moved).

`data/golden/baseline.json` and `data/golden/agent_baseline.json` are the Claude-era baselines, kept for the record and marked `legacy`: the gates refuse them and the agent's drift monitor does not use their costs.
