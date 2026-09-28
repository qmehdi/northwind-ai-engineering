.PHONY: setup setup-aws rollout-aws deployments-aws rotate-key release openapi check-openapi data-manifest check-data audit check test lint fmt preflight session01 session02 data-check train-triage runs promote-triage backtest-triage mlflow-ui serve-triage image-triage images up up-observability down session03 train-semantic export-semantic benchmark runs-semantic promote-semantic backtest-semantic index serve-semantic session04 index-policy eval-policy check-index prompts eval-policy-free feedback-policy calibrate-judge serve-policy session05 agent-eval agent-gate agent-gate-offline approve review replay specialists agent-eval-strands agent-eval-adk mcp

setup:            ## Create the virtualenv and install everything, deep learning and agents included
	uv sync --extra dev --extra dl --extra agents --extra agents-aws --extra agents-gcp --extra mlops
	uv run pre-commit install

check: lint test  ## What CI runs

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

test:             ## The tests this checkout is expected to pass (the skeleton list if present, else everything offline)
	@if [ -f tests/skeleton-green.txt ]; then uv run pytest -q $$(cat tests/skeleton-green.txt); else uv run pytest -q -m "not live"; fi

preflight:        ## Verify the machine and, for aws or gcp tracks, one model round trip
	uv run python scripts/preflight.py

session01:        ## Session 1 acceptance tests
	uv run pytest -q tests/session01

session02:        ## Session 2 acceptance tests
	uv run pytest -q tests/session02

data-check:       ## Project 1: validate and profile the training data
	uv run python -m nw.triage.data_check

train-triage:     ## Project 1: validate, train, record the run, write the card, run the gate
	uv run python -m nw.triage.train

runs:             ## Project 1: the experiment table from artifacts/triage/runs.jsonl
	uv run python -m nw.triage.tracking

promote-triage:   ## Project 1: run the promotion gate on the newest candidate (CANDIDATE=<version>)
	uv run python -m nw.triage.promote $(if $(CANDIDATE),--candidate $(CANDIDATE),)

backtest-triage:  ## Project 1: compare two versions on the test split (A=<dir> B=<dir>)
	uv run python -m nw.triage.backtest --a $(A) --b $(B)

mlflow-ui:        ## Project 1: the MLflow UI on :5000 over artifacts/mlflow.db
	uv run mlflow ui --backend-store-uri sqlite:///artifacts/mlflow.db --port 5000

serve-triage:     ## Run the Project 1 service on :8001
	NW_TRIAGE_MODEL=artifacts/triage/latest uv run uvicorn nw.triage.service:app --port 8001

image-triage:     ## Build the Project 1 container
	docker build --build-arg APP=nw.triage.service:app --build-arg ARTIFACTS="triage" -t nw-triage .

images:           ## Build every service image
	docker compose build

up:               ## Local stack: all services on :8001 to :8020
	docker compose up --build -d

up-observability: ## Local stack plus the OpenTelemetry collector and Jaeger on :16686
	docker compose --profile observability up --build -d

down:             ## Stop the local stack
	docker compose --profile observability down

session03:        ## Session 3 acceptance tests (tiny model, CPU, no download)
	uv run pytest -q tests/session03

train-semantic:   ## Project 2: LoRA fine-tune on a laptop-sized subset, about two minutes on Apple silicon; a registered candidate, no gate
	uv run python -m nw.semantic.train --subset 2000 --epochs 6 --lr 1e-3 --no-promote

export-semantic:  ## Project 2: ONNX export, quantise, parity, into the newest candidate
	uv run python -m nw.semantic.export

benchmark:        ## Project 1 against Project 2, one table, benchmark.json into the newest candidate
	uv run python -m nw.semantic.benchmark

runs-semantic:    ## Project 2: the experiment table from artifacts/semantic/runs.jsonl
	uv run python -m nw.semantic.tracking

promote-semantic: ## Project 2: the gate on the newest candidate (CANDIDATE=<version>); needs export and benchmark first
	uv run python -m nw.semantic.promote $(if $(CANDIDATE),--candidate $(CANDIDATE),)

backtest-semantic: ## Project 2: two versions on the test split, int8 as served (A=<dir> B=<dir>)
	uv run python -m nw.semantic.backtest --a $(A) --b $(B)

index:            ## Build the similar-tickets index
	uv run python -m nw.semantic.embed

serve-semantic:   ## Run the Project 2 service on :8002 from the promoted version
	NW_SEMANTIC_ARTIFACT=artifacts/semantic/latest NW_INDEX=artifacts/index uv run uvicorn nw.semantic.service:app --port 8002

session04:        ## Session 4 acceptance tests
	uv run pytest -q tests/session04

index-policy:     ## Chunk the policy corpus and build the retrieval index
	uv run python -m nw.policy.build_index

check-index:      ## Project 3: exit 1 when artifacts/policy no longer matches data/policies (CI, and before the image build)
	uv run python -m nw.policy.build_index --check

prompts:          ## Every registered prompt with its hash
	uv run python -m nw.llm.prompts

eval-policy-free: ## Project 3: the retrieval-only harness, no model call, gated on recall and MRR
	uv run python -m nw.policy.evaluate --retrieval-only

feedback-policy:  ## Project 3: wrong and unsafe verdicts from /feedback as golden-set candidates
	uv run python -m nw.policy.feedback --to-golden

calibrate-judge:  ## Project 3: judge agreement with human labels (about 0.10 USD)
	uv run python -m nw.policy.calibrate

eval-policy:      ## Run the golden set against the index and apply the regression gate
	uv run python -m nw.policy.evaluate

serve-policy:     ## Run the Project 3 service on :8003
	NW_POLICY_INDEX=artifacts/policy uv run uvicorn nw.policy.service:app --port 8003

session05:        ## Session 5 acceptance tests
	uv run pytest -q tests/session05

agent-eval:       ## The adversarial set through the hand-built loop
	uv run python -m nw.agent.evaluate

agent-gate:       ## Project 4: the 15 cases on your track against data/golden/agent_baseline.json (about 2.5 USD)
	uv run python -m nw.agent.evaluate --gate

agent-gate-offline: ## Project 4: the loop, tools, scorer and gate with a scripted model: free, what CI runs
	uv run python -m nw.agent.evaluate --provider fake --gate --out artifacts/agent_eval_offline.json --traces artifacts/traces-offline

approve:          ## Project 4: pending proposals; make approve RUN=<run_id> TOOL=escalate resumes one with approval
	uv run python -m nw.agent.approve $(if $(RUN),--run $(RUN) --approve $(TOOL),)

review:           ## Project 4: sample ten recent traces into artifacts/review.jsonl for a person to label
	uv run python -m nw.agent.review --sample 10 --out artifacts/review.jsonl

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

# ----- Session 6: deployment. Read deploy/COSTS.md before any of these. -----
TIER ?= session
CANARY ?= 0

release:          ## Cut a release: bump pyproject, move Unreleased in the changelog, relock, commit, tag (VERSION=x.y.z)
	@test -n "$(VERSION)" || { echo "usage: make release VERSION=x.y.z"; exit 1; }
	uv run python scripts/release.py cut $(VERSION)
	uv lock
	git add pyproject.toml uv.lock CHANGELOG.md
	git commit -m "release: v$(VERSION)"
	git tag -a "v$(VERSION)" -m "v$(VERSION)"
	@echo "now: git push origin main v$(VERSION)"

openapi:          ## Refresh docs/openapi/*.json from the apps
	uv run python scripts/openapi_snapshot.py

check-openapi:    ## Exit 1 when an app breaks its committed OpenAPI snapshot (CI)
	uv run python scripts/openapi_snapshot.py --check

data-manifest:    ## Rewrite data/MANIFEST.json (bumps the dataset version when files changed)
	uv run python -m nw.data_manifest

check-data:       ## Exit 1 when data/ changed without the manifest (CI)
	uv run python -m nw.data_manifest --check

audit:            ## pip-audit over the locked set, every extra
	uv export --frozen --no-dev --all-extras --no-emit-project --no-hashes -o requirements.txt
	sed -i.bak -E 's/==([0-9][^+ ;]*)\+[A-Za-z0-9.]+/==\1/' requirements.txt && rm -f requirements.txt.bak
	uv run --with pip-audit pip-audit -r requirements.txt --no-deps --disable-pip --strict

setup-aws:        ## AWS track: the CDK virtualenv and the pinned CDK CLI (Node 22 or later, Docker running)
	uv venv deploy/aws/.venv -p 3.12 && uv pip install -p deploy/aws/.venv/bin/python -r deploy/aws/requirements.txt
	cd deploy/aws && npm install --no-audit --no-fund

session06:        ## Session 6 acceptance tests (router, plus the CDK synth review on the aws track)
	uv run pytest -q tests/session06
	@if [ -x deploy/aws/.venv/bin/python ]; then cd deploy/aws && .venv/bin/python -m pytest -q tests; else echo "aws synth review skipped: no deploy/aws/.venv (gcp track)"; fi

deploy-aws:       ## AWS: synth test, then cdk deploy. TIER=session|reference
	TIER=$(TIER) scripts/deploy_aws.sh deploy

synth-aws:        ## AWS: synth and the review test only
	TIER=$(TIER) scripts/deploy_aws.sh synth

stop-aws:         ## AWS: set reserved concurrency 0 on every northwind function (idle already costs nothing)
	scripts/deploy_aws.sh stop

start-aws:        ## AWS: remove the concurrency limit
	scripts/deploy_aws.sh start

destroy-aws:      ## AWS: delete the tier's stack
	TIER=$(TIER) scripts/deploy_aws.sh destroy

images-gcp:       ## GCP: build and push every image to Artifact Registry
	scripts/images_gcp.sh

deploy-gcp:       ## GCP: validate, plan, apply. TIER=session|reference; CANARY=10 puts the newest revision on 10 percent
	TIER=$(TIER) NW_CANARY=$(CANARY) scripts/deploy_gcp.sh deploy

rollout-aws:      ## AWS: publish $$LATEST of one function and shift the live alias through CodeDeploy. SERVICE=agent
	scripts/deploy_aws.sh rollout $(SERVICE)

deployments-aws:  ## AWS: the last five CodeDeploy deployments of one function. SERVICE=agent
	scripts/deploy_aws.sh deployments $(SERVICE)

rotate-key:       ## Rotate the cohort API key and roll every service. TRACK=aws|gcp
	TRACK=$(TRACK) scripts/rotate_key.sh

plan-gcp:         ## GCP: plan only
	TIER=$(TIER) scripts/deploy_gcp.sh plan

stop-gcp:         ## GCP: scale every northwind Cloud Run service to zero
	scripts/deploy_gcp.sh stop

start-gcp:        ## GCP: restore instance limits
	scripts/deploy_gcp.sh start

destroy-gcp:      ## GCP: terraform destroy the tier
	TIER=$(TIER) scripts/deploy_gcp.sh destroy
