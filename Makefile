.PHONY: package-sagemaker baseline-sagemaker serve-vertex setup setup-local local-up local-down local-pull local-status local-canary local-promote local-higher-up local-bootstrap local-test setup-aws platform-aws-test tenants-aws status-aws gateway-key-aws prompts-catalog rotate-key release openapi check-openapi data-manifest check-data audit check test lint fmt preflight session01 session02 data-check train-triage runs promote-triage backtest-triage mlflow-ui serve-triage image-triage images up up-observability down session03 train-semantic export-semantic benchmark runs-semantic promote-semantic backtest-semantic index serve-semantic session04 index-policy eval-policy check-index prompts eval-policy-free feedback-policy calibrate-judge serve-policy session05 agent-eval agent-gate agent-gate-offline catalog catalog-check roles agent-cards registry registry-check agentops-check approve review replay specialists agent-eval-strands agent-eval-adk mcp pipeline-compile pipeline-run-local pipeline-submit pipeline-definition-aws pipeline-upsert-aws pipeline-upload-gcp images-aws

setup:            ## Create the virtualenv and install everything, deep learning and agents included
	uv sync --extra dev --extra dl --extra agents --extra agents-aws --extra agents-gcp --extra mlops --extra pipelines --extra local --extra platform-aws --extra platform-gcp --extra platform-azure
	uv run pre-commit install

check: lint test  ## What CI runs

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

test:             ## The tests this checkout is expected to pass (the skeleton list if present, else everything offline)
	@if [ -f tests/skeleton-green.txt ]; then uv run pytest -q @tests/skeleton-green.txt; else uv run pytest -q -m "not live"; fi

preflight:        ## Verify the machine and, for aws or gcp tracks, one model round trip
	uv run python scripts/preflight.py

session01:        ## Session 1 acceptance tests
	uv run pytest -q tests/session01

session02:        ## Session 2 acceptance tests
	uv run pytest -q tests/session02

data-check:       ## Project 1: validate and profile the training data
	uv run python -m nw.triage.data_check

# NW_MLFLOW_URI (and the rest of .env) for the targets that log to MLflow, so `make train-triage`
# and the stack share a store when .env points at one (http://localhost:5001 on the Local track).
# Only these targets load it: `make test` and a bare pytest keep their temporary sqlite stores.
# The shell's environment wins over the file (uv run --env-file).
ENV_FILE := $(if $(wildcard .env),--env-file .env,)

train-triage:     ## Project 1: validate, train, record the run, write the card, run the gate (NW_MLFLOW_URI from .env)
	uv run $(ENV_FILE) python -m nw.triage.train

runs:             ## Project 1: the experiment table from artifacts/triage/runs.jsonl
	uv run python -m nw.triage.tracking

promote-triage:   ## Project 1: run the promotion gate on the newest candidate (CANDIDATE=<version>)
	uv run python -m nw.triage.promote $(if $(CANDIDATE),--candidate $(CANDIDATE),)

backtest-triage:  ## Project 1: compare two versions on the test split (A=<dir> B=<dir>)
	uv run python -m nw.triage.backtest --a $(A) --b $(B)

mlflow-ui:        ## Project 1: the MLflow UI on :5000 over NW_MLFLOW_URI from the shell or .env (default artifacts/mlflow.db)
	@uv run $(ENV_FILE) sh -c 'uri="$${NW_MLFLOW_URI:-sqlite:///artifacts/mlflow.db}"; case "$$uri" in \
	  http://*|https://*) echo "$$uri is an MLflow server and serves its own UI: open $$uri" ;; \
	  *) exec mlflow ui --backend-store-uri "$$uri" --port 5000 ;; \
	esac'

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

train-semantic:   ## Project 2: LoRA fine-tune on a laptop-sized subset, about two minutes on Apple silicon; a registered candidate, no gate (NW_MLFLOW_URI from .env)
	uv run $(ENV_FILE) python -m nw.semantic.train --subset 2000 --epochs 6 --lr 1e-3 --no-promote

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

# ----- the training pipelines (ADR 0008, 0011): shared steps, Kubeflow and SageMaker definitions -----
PIPELINE ?= triage
PIPELINE_IMAGE ?= nw-pipelines:latest
PIPELINE_RUNNER ?= subprocess

pipeline-compile:  ## Compile both pipelines to artifacts/pipelines/*.yaml for the given image (PIPELINE_IMAGE=<tag>)
	uv run python -m nw.pipelines.kfp.compile --image $(PIPELINE_IMAGE)

pipeline-run-local: ## Run a pipeline on the Kubeflow local runner (PIPELINE=triage|semantic, PIPELINE_RUNNER=subprocess|docker, SET="epochs=1 subset=500")
	uv run python -m nw.pipelines.kfp.run_local --pipeline $(PIPELINE) --runner $(PIPELINE_RUNNER) --image $(PIPELINE_IMAGE) $(foreach kv,$(SET),--set $(kv))

pipeline-submit:   ## Submit a pipeline through the active track's platform (PIPELINE=triage|semantic, ARGS="--epochs 2 --wait")
	uv run python -m nw.pipelines.retrain --pipeline $(PIPELINE) $(ARGS)

pipeline-definition-aws: ## Print the SageMaker definition JSON without calling AWS (ROLE, IMAGE, BUCKET, TENANT)
	uv run python -m nw.pipelines.sagemaker.definition --pipeline $(PIPELINE) --role $(ROLE) --image $(IMAGE) --bucket $(BUCKET) --tenant $(or $(TENANT),solo)

pipeline-upsert-aws: ## AWS: create or update the tenant's SageMaker pipeline northwind-<tenant>-<PIPELINE> (NW_AWS_PIPELINE_IMAGE)
	uv run python -m nw.platform.aws pipeline-upsert $(PIPELINE)

pipeline-upload-gcp: ## GCP: compile for PIPELINE_IMAGE and upload every pipeline to gs://<artifacts>/<prefix>/pipelines/ (TENANTS=alice,bob)
	uv run python -m nw.pipelines.kfp.compile --image $(PIPELINE_IMAGE)
	uv run python -m nw.platform.gcp pipeline-upload --tenants "$(or $(TENANTS),$(NW_TENANTS))"

# ----- serving on the platform (ADR 0008): SageMaker artifacts and the Vertex contract -----
package-sagemaker: ## SageMaker: model.tar.gz with code/ for the prebuilt container (ARTIFACT=artifacts/triage/latest INTO=dist/triage)
	uv run python -m nw.serving.sagemaker.package $(or $(ARTIFACT),artifacts/triage/latest) --into $(or $(INTO),dist/triage)

baseline-sagemaker: ## SageMaker: Model Monitor statistics.json and constraints.json from the training profile (PROFILE, INTO, TICKETS=data/tickets.jsonl)
	uv run python -m nw.serving.sagemaker.baseline --profile $(or $(PROFILE),artifacts/triage/latest/data_profile.json) --out $(or $(INTO),artifacts/triage/latest/baseline) $(if $(TICKETS),--tickets $(TICKETS),)

serve-vertex:     ## Run the Project 1 service under the Agent Platform contract on :8080 (/health, /predict), from NW_MODEL_URI or the fallback
	AIP_HTTP_PORT=8080 uv run uvicorn nw.triage.service:app --port 8080

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

agent-eval:       ## The adversarial set through the hand-built loop, every check labelled with its tier
	uv run python -m nw.agent.evaluate

agent-gate:       ## Project 4: the 15 cases on your track against the tier bars in data/golden/agent_baseline.json (about 3 USD with the Judge)
	uv run python -m nw.agent.evaluate --gate

agent-gate-offline: ## Project 4: the loop, tools, scorer and tiered gate with a scripted model: free, what CI runs
	uv run python -m nw.agent.evaluate --provider fake --gate --out artifacts/agent_eval_offline.json --traces artifacts/traces-offline

catalog:          ## AgentOps: the use case catalog (data/use_cases.yaml) as a table; make roles for who owns which step
	uv run python -m nw.agent.catalog

catalog-check:    ## AgentOps: every agent in code has an approved use case that allows its tools
	uv run python -m nw.agent.catalog --check

roles:            ## AgentOps: the lifecycle roles and the step each owns (the guide embeds this)
	uv run python -m nw.agent.catalog --roles

agent-cards:      ## AgentOps: write data/agents/<name>.json from code; PUSH=1 registers them with your track's runtime
	uv run python -m nw.agent.registry --write
	$(if $(PUSH),uv run python -m nw.agent.registry --push,)

registry:         ## AgentOps: the agent cards on disk; make registry-check proves they match the code
	uv run python -m nw.agent.registry --list

registry-check:   ## AgentOps: every card matches the code it describes: tools, prompt hash, version
	uv run python -m nw.agent.registry --check

agentops-check:   ## AgentOps: catalog, registry and the offline tiered gate, what agent-gate.yml runs
	$(MAKE) catalog-check registry-check agent-gate-offline

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

# ----- Local track (ADR 0011): the compose platform. deploy/local/README.md has ports and RAM. -----
WEIGHT ?= 10

setup-local:      ## Local track: the platform clients (MLflow, kfp local runner, Qdrant) into the virtualenv
	uv sync --extra dev --extra dl --extra agents --extra mlops --extra local
	@docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required (Docker Desktop or the docker-compose-plugin)"; exit 1; }
	@echo "next: make local-up (16 GB for Docker; the first start pulls about 16 GB of models)"

local-up:         ## Local track: build (the nw-pipelines image too), push to the local registry on :5050, start platform and observability, register the local triage model
	deploy/local/local.sh up

local-down:       ## Local track: stop every profile, keep the volumes (models, registry, MLflow state)
	deploy/local/local.sh down

local-pull:       ## Local track: pull the Ollama models again (GPU=1 adds gpt-oss:120b)
	deploy/local/local.sh pull

local-status:     ## Local track: every container's health, what is live, the canary weights
	deploy/local/local.sh status

local-canary:     ## Local track: WEIGHT percent of the endpoint's traffic to the canary replica (make local-canary WEIGHT=10)
	uv run python -m nw.platform.local canary $(WEIGHT)

local-promote:    ## Local track: copy the live triage version into the higher environment and make it live there
	uv run python -m nw.platform.local promote

local-higher-up:  ## Local track: the higher environment's serving tier on :8105 (the promotion target)
	deploy/local/local.sh higher-up

local-bootstrap:  ## Local track: register artifacts/triage/latest and make it live (local-up does this once)
	deploy/local/local.sh bootstrap

local-test:       ## Local track: the platform tests, offline ones and the live ones against the running stack
	uv run pytest -q tests/platform -m "not live"
	uv run pytest -q tests/platform -m live

setup-aws:        ## AWS track: the CDK virtualenv and the pinned CDK CLI (Node 22 or later, Docker running)
	uv venv deploy/aws/.venv -p 3.12 && uv pip install -p deploy/aws/.venv/bin/python -r deploy/aws/requirements.txt
	cd deploy/aws && npm install --no-audit --no-fund

session06:        ## Session 6 acceptance tests (router, plus the CDK synth review on the aws track)
	uv run pytest -q tests/session06
	@if [ -x deploy/aws/.venv/bin/python ]; then cd deploy/aws && .venv/bin/python -m pytest -q tests; else echo "aws synth review skipped: no deploy/aws/.venv (gcp track)"; fi

platform-aws-test: ## AWS platform: the CDK synth review and the client tests, no account needed
	uv run pytest -q tests/platform -k aws
	cd deploy/aws && .venv/bin/python -m pytest -q tests

synth-aws:        ## AWS: refresh the prompt catalog, run the synth review, cdk synth (NW_MODE=solo | NW_TENANTS=a,b)
	scripts/deploy_aws.sh synth

deploy-aws:       ## AWS: synth review, bootstrap, cdk deploy, virtual keys, policy corpus. Read deploy/COSTS-platform.md first
	scripts/deploy_aws.sh deploy

tenants-aws:      ## AWS: list the tenants; TENANT=alice ACTION=add|remove changes the cohort
	scripts/deploy_aws.sh tenants $(ACTION) $(TENANT)

status-aws:       ## AWS: what is up (endpoints, runtimes, MLflow, gateway)
	scripts/deploy_aws.sh status

release-aws:      ## AWS: start the delivery pipeline on this commit; it stops at the Approve stage
	scripts/deploy_aws.sh release

approve-aws:      ## AWS: approve the waiting Promote action (REASON="..." for the record)
	REASON="$(REASON)" scripts/deploy_aws.sh approve

stop-aws:         ## AWS: idle cost to the floor (live endpoints, MLflow, gateway task, policy concurrency)
	scripts/deploy_aws.sh stop

start-aws:        ## AWS: bring the platform back
	scripts/deploy_aws.sh start

destroy-aws:      ## AWS: delete the platform (endpoints, schedules and tenant runtimes first)
	scripts/deploy_aws.sh destroy

gateway-key-aws:  ## AWS: mint the model gateway key for one owner. TENANT=alice [BUDGET=25]
	deploy/aws/scripts/gateway_keys.sh $(TENANT) $(BUDGET)

prompts-catalog:  ## Export the prompt registry to deploy/aws/prompts/catalog.json (the stack reads it)
	uv run python -m nw.platform.aws prompts-catalog

images-aws:       ## AWS: build and push policy, agent (arm64) and the pipelines image (amd64) to the stack's ECR, for a deploy without the delivery pipeline (IMAGES="pipelines")
	scripts/images_aws.sh

images-gcp:       ## GCP: build and push every image, the pipelines image included, to Artifact Registry
	scripts/images_gcp.sh

deploy-gcp:       ## GCP: validate, plan, apply the platform. NW_TENANTS=alice,bob or NW_MODE=solo
	scripts/deploy_gcp.sh apply

rotate-key:       ## Rotate the cohort API key and roll every service. TRACK=aws|gcp
	TRACK=$(TRACK) scripts/rotate_key.sh

validate-gcp:     ## GCP: terraform fmt, init and validate the platform root
	scripts/deploy_gcp.sh validate

plan-gcp:         ## GCP: plan only
	scripts/deploy_gcp.sh plan

tenants-gcp:      ## GCP: every tenant's URLs, engine, corpus and secrets
	scripts/deploy_gcp.sh tenants

status-gcp:       ## GCP: Cloud Run, Agent Engine, live endpoints, pipeline runs and rollouts
	scripts/deploy_gcp.sh status

keys-gcp:         ## GCP: register every tenant's gateway key and budget with the LiteLLM proxy
	scripts/deploy_gcp.sh keys

prompts-gcp:      ## GCP: register the course prompts for one tenant. TENANT=alice
	uv run python scripts/gcp_prompts.py --project $(NW_GCP_PROJECT) --bucket $(NW_ENVIRONMENT)-$(NW_GCP_PROJECT)-artifacts --tenant $(TENANT)

release-gcp:      ## GCP: a Cloud Deploy release of the images tagged with the git SHA; stops at the canary
	scripts/deploy_gcp.sh release

approve-gcp:      ## GCP: approve the rollout waiting at the canary
	scripts/deploy_gcp.sh approve

stop-gcp:         ## GCP: scale every service to zero, undeploy live models, stop Cloud SQL
	scripts/deploy_gcp.sh stop

start-gcp:        ## GCP: restore instance limits and Cloud SQL
	scripts/deploy_gcp.sh start

destroy-gcp:      ## GCP: terraform destroy the platform, then the live services Cloud Deploy made
	scripts/deploy_gcp.sh destroy

platform-gcp-test: ## GCP: the platform client tests and the Terraform validate and plan tests
	uv run pytest -q tests/platform/test_gcp_platform.py tests/platform/test_gcp_terraform.py

.PHONY: setup-azure platform-azure-test describe-azure pipeline-definition-azure
setup-azure:      ## Azure track: the platform clients (Azure ML, Foundry projects, AI Search, Blob, identity) into the virtualenv, and the Bicep CLI
	uv sync --extra dev --extra platform-azure
	scripts/deploy_azure.sh setup

platform-azure-test: ## Azure: the platform client, naming, provider and Azure ML pipeline tests, no subscription needed
	uv run pytest -q tests/platform/test_azure_platform.py tests/platform/test_azure_naming.py tests/session01/test_providers_azure.py tests/pipelines/test_azureml.py

describe-azure:   ## Azure: what the platform client resolved (NW_AZURE_* and deploy/azure/outputs.json) and the tenant's resource names
	uv run python -m nw.platform.azure describe

pipeline-definition-azure: ## Azure: the tenant's Azure ML pipeline job as YAML, without calling Azure (PIPELINE=triage|semantic)
	uv run python -m nw.platform.azure pipeline-definition $(or $(PIPELINE),triage)

.PHONY: build-azure what-if-azure deploy-azure tenants-azure status-azure stop-azure start-azure destroy-azure images-azure release-azure approve-azure keys-azure indexes-azure bicep-azure-test
build-azure:      ## Azure: bicep build and lint deploy/azure/main.bicep, no Azure call
	scripts/deploy_azure.sh build

what-if-azure:    ## Azure: what the deployment would change (reads the subscription). NW_TENANTS=alice,bob or NW_MODE=solo
	scripts/deploy_azure.sh what-if

deploy-azure:     ## Azure: deploy the platform, upload data and baselines, create the search indexes. Read deploy/COSTS-platform.md first
	scripts/deploy_azure.sh deploy

tenants-azure:    ## Azure: what each tenant got; ACTION=add|remove TENANT=carol changes the cohort
	scripts/deploy_azure.sh tenants $(ACTION) $(TENANT)

status-azure:     ## Azure: endpoints, apps and traffic, model deployments, pipeline jobs, schedules, fired alerts
	scripts/deploy_azure.sh status

stop-azure:       ## Azure: delete the online deployments, idle the apps, stop the LiteLLM database
	scripts/deploy_azure.sh stop

start-azure:      ## Azure: start the LiteLLM database; endpoints refill on the next approval
	scripts/deploy_azure.sh start

destroy-azure:    ## Azure: delete the resource group, purge soft-deleted names, remove the custom roles
	scripts/deploy_azure.sh destroy

images-azure:     ## Azure: build and push every image, the pipelines image included, to the platform registry (IMAGES="policy agent")
	scripts/images_azure.sh

release-azure:    ## Azure: a new revision of the live policy and agent apps by digest at the canary weight
	scripts/deploy_azure.sh release

approve-azure:    ## Azure: give the canary revisions all the traffic unless a live alert fires (REASON="...")
	REASON="$(REASON)" scripts/deploy_azure.sh approve

keys-azure:       ## Azure: register the owners' keys with LiteLLM (gateway kind litellm); APIM keys need nothing
	scripts/deploy_azure.sh keys

indexes-azure:    ## Azure: create the per-owner search indexes and their index-scoped roles again
	scripts/deploy_azure.sh indexes

bicep-azure-test: ## Azure: the Bicep build, lint and naming tests, no subscription needed
	uv run pytest -q tests/platform/test_azure_bicep.py
