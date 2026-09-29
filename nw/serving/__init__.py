"""The serving layer: what a course service needs to run on a managed platform (ADR 0008).

- `download`: the model artifact from `NW_MODEL_URI` (`s3://`, `gs://`, `file://`, MLflow
  `models:/`) into a temp directory at startup, with `artifacts/<project>/latest` as the fallback.
- `identity`: the tenant and environment every log line, drift alert and metrics line carries.
- `gateway`: the model gateway key from the track's secret store when it is not in the
  environment, the way `nw.auth` resolves the API key.
- `vertex`: the Agent Platform (formerly Vertex AI) custom container contract as routes on a
  service app, beside the course's own routes.
- `sagemaker`: the inference handler, the `model.tar.gz` packager and the Model Monitor
  baseline for the SageMaker prebuilt containers.
"""
