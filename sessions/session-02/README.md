# Machine learning in production

What you build: Project 1, the ticket triage scoring service in `nw/triage`, and the MLOps loop around it: a data contract, a promotion gate, run tracking, a model card, drift monitoring, shadow mode and a retraining workflow.

```bash
make session02                                          # 59 tests
make pipeline-run-local PIPELINE=triage                 # data check, train, gate, register on the Kubeflow local runner
make pipeline-submit PIPELINE=triage [ARGS="--min-p0-recall 0.9"]   # the same pipeline on your track's platform
make pipeline-definition-aws PIPELINE=triage ROLE=... IMAGE=... BUCKET=... TENANT=...   # AWS: the SageMaker definition, no call
make package-sagemaker / baseline-sagemaker / serve-vertex   # the registry's packaging per track
make data-check                                         # validate data/tickets.jsonl, write the data profile
make train-triage                                       # validate, train, calibrate, record, card, gate; exit 1 on GATE FAILED
make runs                                               # the experiment table from artifacts/triage/runs.jsonl
make promote-triage [CANDIDATE=<version>]               # the gate on demand; --force is recorded
make mlflow-ui                                          # the same runs and registry on :5002 (a server URI opens itself)
make serve-triage                                       # the service on :8001, /drift included
make backtest-triage A=artifacts/triage/latest B=<dir>  # two versions on the test split
make source-bundle                                      # the bundle of nw/ a cloud pipeline submit ships
make drift-triage-central CAPTURE="captures/*.jsonl"    # drift over every instance's capture
make quality-triage CAPTURE=... OUTCOMES=...            # live P0 recall from labels at least 3 days old, joined on ticket_id or correlation_id
make retrain-trigger [NEW_LABELS=600]                   # retrain only for a reason; same data and no signal skips
make bootstrap-aws|gcp|azure NAMES="triage"             # recovery: register and approve artifacts/triage/latest
```

Run order in the part: `data-check`; `train-triage` after each of the four model changes (class weights, the threshold, calibration on its own half of validation; the gate passes on calibration, German waived); `runs` and `promote-triage` on the fifth run; the card and `promotions.jsonl`; the pipeline (`pipeline-run-local`, `pipeline-submit`), the registry (`platform.registry.versions`), the approval (`set_stage(..., Stage.APPROVED, reason)` on the newest candidate) and the endpoint; the service with `/drift`, a shadow and `backtest-triage`; the central drift job, online quality and the retrain trigger; `image-triage` and the per-track packaging; a tightened bar through the pipeline.

Files you edit in this part:

- `nw/triage/train.py`: `build_pipeline` (class weights), `choose_p0_threshold`, the calibration block in `train`
- `nw/triage/model.py`: `TriageModel.decide`
- `nw/triage/service.py`: the model-loading block in `lifespan`

Files you read but do not edit:

- `nw/triage/features.py`, the rest of `service.py` (drift, shadow, capture), `Dockerfile`
- `nw/triage/data_check.py`: the row contract, the expectations, the data profile
- `nw/triage/promote.py`: the gate policy (counts, paired comparison, per-language slices, waivers) and the promotion log
- `nw/evalstats.py`: Wilson intervals, McNemar, the paired bootstrap, the PSI chance level
- `nw/triage/tracking.py`: `runs.jsonl` and the MLflow experiment and registry
- `nw/triage/model_card.py`: `MODEL_CARD.md` from the artifact's metadata
- `nw/triage/monitor.py`: PSI and the drift window, the quality gauges, the central drift job, online quality and the retrain trigger
- `nw/triage/backtest.py`: two versions on the same tickets
- `.github/workflows/retrain-triage.yml`: weekly behind the trigger, on demand and on pull requests; never promotes on its own
- `data/golden/triage_slices.jsonl`: the held-out P0 and German tickets the per-language bars read
- `data/golden/triage_production.json`: the production summary the gate compares against in CI; rewritten on every promotion and committed

Environment for the service, all optional: `NW_TRIAGE_SHADOW_MODEL`, `NW_TRIAGE_CAPTURE`, `NW_TRIAGE_DRIFT_WINDOW` (500), `NW_TRIAGE_DRIFT_MIN` (200), `NW_TRIAGE_DRIFT_EVERY` (50). MLflow is the `mlops` extra; `make setup` installs it, and `NW_MLFLOW_URI` overrides the sqlite store.
