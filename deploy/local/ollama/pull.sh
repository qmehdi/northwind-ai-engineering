#!/bin/sh
# Pull the models the platform profile needs, smallest first so the guardrail is usable while
# the Workhorse downloads. Runs once per `up` against the ollama container; a pull of a model
# that is already in the store returns at once. NW_OLLAMA_PULL lists the tags; unset pulls the
# defaults, set and empty pulls nothing (the models are already in a native Ollama).
set -eu
: "${OLLAMA_HOST:=http://ollama:11434}"
: "${NW_OLLAMA_PULL=llama-guard3:1b gpt-oss:20b}"
export OLLAMA_HOST
for m in $NW_OLLAMA_PULL; do
  echo "pulling $m"
  ollama pull "$m"
done
ollama list
