# Session 2: Machine learning in production

What you build: Project 1, the ticket triage scoring service in `nw/triage`.

```bash
make session02
uv run python -m nw.triage.train                       # trains, calibrates, saves artifacts/triage/<version>
NW_TRIAGE_MODEL=artifacts/triage/latest uv run uvicorn nw.triage.service:app --port 8001
```

Files you edit in this session:

- `nw/triage/train.py`: `build_pipeline` (class weights), `choose_p0_threshold`, the calibration block in `train`
- `nw/triage/model.py`: `TriageModel.decide`
- `nw/triage/service.py`: the model-loading block in `lifespan`

Files you read but do not edit: `nw/triage/features.py`, the rest of `service.py`, `Dockerfile`.
