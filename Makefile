.PHONY: setup check test lint fmt preflight session01 session02 train-triage serve-triage image-triage session03 train-semantic export-semantic benchmark index serve-semantic session04 index-policy eval-policy serve-policy session05 agent-eval replay specialists agent-eval-strands agent-eval-adk mcp

setup:            ## Create the virtualenv and install everything, deep learning and agents included
	uv sync --extra dev --extra dl --extra agents --extra agents-aws --extra agents-gcp
	uv run pre-commit install

check: lint test  ## What CI runs

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

test:             ## Every test that runs without cloud credentials
	uv run pytest -q -m "not live"

preflight:        ## Verify the machine and, for aws or gcp tracks, one model round trip
	uv run python scripts/preflight.py

session01:        ## Session 1 acceptance tests
	uv run pytest -q -m session01

session02:        ## Session 2 acceptance tests
	uv run pytest -q -m session02

train-triage:     ## Train the Project 1 model into artifacts/triage/<version>
	uv run python -m nw.triage.train

serve-triage:     ## Run the Project 1 service on :8001
	NW_TRIAGE_MODEL=artifacts/triage/latest uv run uvicorn nw.triage.service:app --port 8001

image-triage:     ## Build the Project 1 container
	docker build --build-arg APP=nw.triage.service:app --build-arg MODEL=artifacts/triage/latest -t nw-triage .

session03:        ## Session 3 acceptance tests (tiny model, CPU, no download)
	uv run pytest -q -m session03

train-semantic:   ## LoRA fine-tune on a laptop-sized subset, about two minutes on Apple silicon
	uv run python -m nw.semantic.train --subset 2000 --epochs 6 --lr 1e-3

export-semantic:  ## ONNX export, quantise, parity
	uv run python -m nw.semantic.export

benchmark:        ## Project 1 against Project 2, one table
	uv run python -m nw.semantic.benchmark

index:            ## Build the similar-tickets index
	uv run python -m nw.semantic.embed

serve-semantic:   ## Run the Project 2 service on :8002
	NW_SEMANTIC_ARTIFACT=artifacts/semantic NW_INDEX=artifacts/index uv run uvicorn nw.semantic.service:app --port 8002

session04:        ## Session 4 acceptance tests
	uv run pytest -q -m session04

index-policy:     ## Chunk the policy corpus and build the retrieval index
	uv run python -m nw.policy.build_index

eval-policy:      ## Run the golden set against the index and apply the regression gate
	uv run python -m nw.policy.evaluate

serve-policy:     ## Run the Project 3 service on :8003
	NW_POLICY_INDEX=artifacts/policy uv run uvicorn nw.policy.service:app --port 8003

session05:        ## Session 5 acceptance tests
	uv run pytest -q -m session05

agent-eval:       ## The adversarial set through the hand-built loop
	uv run python -m nw.agent.evaluate

replay:           ## Print one trajectory: make replay RUN=<run_id>
	uv run python -m nw.agent.trace artifacts/traces/$(RUN).json

specialists:      ## Three specialists and the orchestrator, foreground, Ctrl-C stops all
	uv run python -m nw.agent.launch

agent-eval-strands: ## AWS track: same evaluation through Strands
	uv run python -m nw.agent.ports.strands_port

agent-eval-adk:   ## GCP track: same evaluation through ADK
	uv run python -m nw.agent.ports.adk_port

mcp:              ## The tool registry as an MCP server on :8020
	uv run python -m nw.agent.mcp_server
