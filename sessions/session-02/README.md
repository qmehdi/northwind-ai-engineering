# Session 2: Machine learning in production

What you build: Project 1, the ticket triage scoring service in `nw/triage`, and the MLOps loop around it: a data contract, a promotion gate, run tracking, a model card, drift monitoring, shadow mode and a retraining workflow.

```bash
make session02                                          # 29 tests
make data-check                                         # validate data/tickets.jsonl, write the data profile
make train-triage                                       # validate, train, calibrate, record, card, gate; exit 1 on GATE FAILED
make runs                                               # the experiment table from artifacts/triage/runs.jsonl
make promote-triage [CANDIDATE=<version>]               # the gate on demand; --force is recorded
make mlflow-ui                                          # the same runs and registry on :5000
make serve-triage                                       # the service on :8001, /drift included
make backtest-triage A=artifacts/triage/latest B=<dir>  # two versions on the test split
```

Run order in the session: `data-check`, then `train-triage` after each of the four model changes (the gate passes on the fourth), `runs` and `mlflow-ui`, the card and `promotions.jsonl`, the service with `/drift`, optionally a `--no-promote --target-recall 0.95` candidate as `NW_TRIAGE_SHADOW_MODEL` and `backtest-triage`, then `image-triage`.

Files you edit in this session:

- `nw/triage/train.py`: `build_pipeline` (class weights), `choose_p0_threshold`, the calibration block in `train`
- `nw/triage/model.py`: `TriageModel.decide`
- `nw/triage/service.py`: the model-loading block in `lifespan`

Files you read but do not edit:

- `nw/triage/features.py`, the rest of `service.py` (drift, shadow, capture), `Dockerfile`
- `nw/triage/data_check.py`: the row contract, the expectations, the data profile
- `nw/triage/promote.py`: the gate policy and the promotion log
- `nw/triage/tracking.py`: `runs.jsonl` and the MLflow experiment and registry
- `nw/triage/model_card.py`: `MODEL_CARD.md` from the artifact's metadata
- `nw/triage/monitor.py`: PSI and the drift window
- `nw/triage/backtest.py`: two versions on the same tickets
- `.github/workflows/retrain-triage.yml`: weekly, on demand and on pull requests; never promotes on its own
- `data/golden/triage_production.json`: the production summary the gate compares against in CI; rewritten on every promotion and committed

Environment for the service, all optional: `NW_TRIAGE_SHADOW_MODEL`, `NW_TRIAGE_CAPTURE`, `NW_TRIAGE_DRIFT_WINDOW` (500), `NW_TRIAGE_DRIFT_MIN` (50), `NW_TRIAGE_DRIFT_EVERY` (50). MLflow is the `mlops` extra; `make setup` installs it, and `NW_MLFLOW_URI` overrides the sqlite store.
