# Session 3: Deep learning and representation

What you build: Project 2, the semantic understanding engine in `nw/semantic`.

```bash
make session03
make train-semantic          # LoRA fine-tune on a 2,000 ticket subset, a few minutes on a laptop
make export-semantic         # ONNX export, int8 quantisation, parity check
make benchmark               # Project 1 against Project 2 in three forms, one table
make index                   # embed the training tickets into artifacts/index
make serve-semantic          # the service on :8002
```

Files you edit in this session:

- `nw/semantic/model.py`: `apply_lora`
- `nw/semantic/train.py`: `loss_fn`, the training step, `tune_tag_thresholds`, `save_checkpoint`
- `nw/semantic/export.py`: `export_onnx`

Files you read but do not edit: `data.py`, `embed.py`, `benchmark.py`, `service.py`.
