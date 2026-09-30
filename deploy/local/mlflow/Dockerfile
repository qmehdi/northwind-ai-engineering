# The MLflow tracking server with registry, prompts and artifact proxy (ADR 0011).
# The upstream image ships no S3 or Postgres driver; this adds both so the server keeps
# artifacts on the object store and its state in Postgres. Digest fetched 2026-09-29.
FROM ghcr.io/mlflow/mlflow:v3.16.1@sha256:06058cc872276873e9759c635e773342aca4731e240f6d20d88ea43774083984
# Slow or flaky networks: longer timeouts and retries for every index and wheel request.
ENV PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10
RUN pip install --no-cache-dir boto3==1.43.89 psycopg2-binary==2.9.13
COPY init_bucket.py /usr/local/bin/init_bucket.py
