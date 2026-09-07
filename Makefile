.PHONY: setup check test lint fmt preflight session01

setup:            ## Create the virtualenv and install everything
	uv sync --extra dev
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
