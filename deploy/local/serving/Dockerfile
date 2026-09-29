# The serving tier: MLflow model serving over the course image, so the registered pyfunc can
# import `nw` (the triage pipeline pickles nw.triage.features transformers). One image serves
# every registered version; which one is decided by the alias the container is started with.
#   docker compose build serving-stable      (BASE defaults to the registry's nw-triage image)
ARG BASE=localhost:5050/nw-triage:dev
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424
FROM ${UV_IMAGE} AS uv
FROM ${BASE}
USER root
COPY --from=uv /uv /bin/uv
RUN uv pip install --python /app/.venv/bin/python mlflow==3.16.1 && rm /bin/uv
COPY serve.sh /usr/local/bin/serve.sh
RUN chmod +x /usr/local/bin/serve.sh && chown -R nw:nw /app
USER nw
ENV NW_SERVING_MODEL=northwind-solo-triage NW_SERVING_ALIAS=live MLFLOW_TRACKING_URI=http://mlflow:5000
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1
CMD ["/usr/local/bin/serve.sh"]
