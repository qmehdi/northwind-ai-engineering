-- One Postgres, two databases: MLflow's backend store and LiteLLM's key and spend tables.
-- Runs once, on the first start of an empty volume.
CREATE DATABASE litellm;
