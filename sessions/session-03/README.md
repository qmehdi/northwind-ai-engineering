# Session 3: Deep learning and representation

What you build: Project 2, the semantic understanding engine in `nw/semantic`, inside the same loop as Project 1: the data contract, versioned artifacts, run tracking, a promotion gate fed by the export and the benchmark, a model card, three drift signals in the service, a backtest between versions, and a retraining workflow.

```bash
make session03                                              # 28 tests, tiny model, no download
make data-check                                             # the same contract as Project 1, same file
make train-semantic                                         # LoRA fine-tune, 2,000 ticket subset, six epochs, about two minutes on Apple silicon; writes artifacts/semantic/<version>/, no gate
make runs-semantic                                          # the experiment table from artifacts/semantic/runs.jsonl
make mlflow-ui                                              # the same runs, experiment `semantic`, registered model `northwind-semantic`
make export-semantic                                        # ONNX export, int8 quantisation, parity, into the newest candidate
make benchmark                                              # Project 1 against Project 2 in three forms; benchmark.json into the newest candidate
make promote-semantic [CANDIDATE=<version>]                 # the gate: reads metadata, export report and benchmark; moves latest; --force is recorded
make index                                                  # embed the training tickets into artifacts/index
make serve-semantic                                         # the service on :8002 from artifacts/semantic/latest, /drift included
make backtest-semantic A=artifacts/semantic/latest B=<dir>  # two versions on the test split, int8 as served
```

Run order in the session: `data-check`, `train-semantic`, `runs-semantic`, then `export-semantic`, `benchmark`, `promote-semantic` (the gate passes and `latest` appears), the card and `promotions.jsonl`, optionally the service with `/drift`, a two-epoch `--no-promote` candidate through `backtest-semantic` and the gate (it fails), and the workflow. `python -m nw.semantic.train` without `--no-promote` runs export, benchmark and the gate itself after training.

Files you edit in this session:

- `nw/semantic/model.py`: `apply_lora`
- `nw/semantic/train.py`: `loss_fn`, the training step, `tune_tag_thresholds`, `save_checkpoint`
- `nw/semantic/export.py`: `export_onnx`

Files you read but do not edit:

- `nw/semantic/data.py`, `embed.py`, `benchmark.py`, `service.py` (drift, capture, `latest` resolution)
- `nw/semantic/artifacts.py`: which directory a path means: a version, `latest`, a flat directory, or the root
- `nw/semantic/promote.py`: the gate policy and the promotion log
- `nw/semantic/tracking.py`: `runs.jsonl` and the MLflow experiment and registry
- `nw/semantic/model_card.py`: `MODEL_CARD.md` from the artifact's files
- `nw/semantic/monitor.py`: Project 1's two signals plus the tag rate
- `nw/semantic/backtest.py`: two versions on the same tickets, as served
- `.github/workflows/retrain-semantic.yml`: on demand only (CPU runners); never promotes on its own
- `data/golden/semantic_production.json`: the production summary the gate compares against in CI; rewritten on every promotion and committed

Environment for the service, all optional: `NW_SEMANTIC_ARTIFACT` (a version, `latest`, a flat directory, or the root, which means `latest`), `NW_QUANTIZED` (1), `NW_SEMANTIC_CAPTURE`, `NW_SEMANTIC_DRIFT_WINDOW` (500), `NW_SEMANTIC_DRIFT_MIN` (50), `NW_SEMANTIC_DRIFT_EVERY` (50). MLflow is the `mlops` extra; `NW_MLFLOW_URI` overrides the sqlite store.
