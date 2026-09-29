"""SageMaker serving for Projects 1 and 2 on the prebuilt framework containers.

A registered model package needs an x86_64 framework container and a `model.tar.gz` whose
`code/` directory holds `inference.py` and `requirements.txt` (the SageMaker inference toolkit
contract: `model_fn`, `input_fn`, `predict_fn`, `output_fn`). Three modules:

- `inference`: the handler both containers run, around the triage `model.py` and the semantic
  ONNX graph.
- `package`: writes `code/` and builds the tarball, so what the training step packages is a
  valid model artifact.
- `baseline`: `statistics.json` and `constraints.json` for Model Monitor, with the
  `text_length` feature the platform's drift alarm watches; `preprocessor` turns a captured
  request into that feature.

Prebuilt images (SageMaker Python SDK `image_uri_config`, master, fetched 2026-09-29; the tag
is `<version>-cpu-py3` for scikit-learn and `<version>-cpu-<py>-ubuntu22.04-sagemaker` for
PyTorch, both x86_64):

- triage, scikit-learn 1.9 (the version `uv.lock` pins, which the joblib pickle needs):
  `683313688378.dkr.ecr.us-east-1.amazonaws.com/sagemaker-scikit-learn:1.9-0-cpu-py3`
  (`1.4-2-py312-cpu-py3` is the previous release on the same registry)
- semantic, PyTorch inference 2.6 CPU (onnxruntime and transformers come from
  `requirements.txt` at startup):
  `763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-inference:2.6.0-cpu-py312-ubuntu22.04-sagemaker`

Other regions use the same repositories under the account of the region's registry (the
`registries` map of the same config files). Set them as `NW_AWS_IMAGE_TRIAGE` and
`NW_AWS_IMAGE_SEMANTIC` for the registry step, or pass `tags["image"]` to `register`.
"""

SKLEARN_IMAGE = "683313688378.dkr.ecr.us-east-1.amazonaws.com/sagemaker-scikit-learn:1.9-0-cpu-py3"
PYTORCH_IMAGE = (
    "763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-inference:"
    "2.6.0-cpu-py312-ubuntu22.04-sagemaker"
)
IMAGES = {"triage": SKLEARN_IMAGE, "semantic": PYTORCH_IMAGE}
IMAGES_FETCHED = "2026-09-29"
