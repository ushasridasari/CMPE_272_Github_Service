# Developer entry points. `make help` lists everything.

.DEFAULT_GOAL := help
SHELL := /bin/bash
PORT ?= 8080
PY   ?= python

.PHONY: help install run dev test test-unit test-integration test-webhook \
        coverage lint fmt spec-lint docker-build docker-run compose-up \
        compose-tunnel secret clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime + dev dependencies
	$(PY) -m pip install -r requirements-dev.txt

run: ## Run the service (reads .env)
	$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port $(PORT) \
		--log-config=/dev/null --no-access-log

dev: ## Run with autoreload
	$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port $(PORT) --reload

test: test-unit ## Alias for the offline test suite

test-unit: ## Unit tests with coverage gate (no network)
	$(PY) -m pytest tests/unit -v \
		--cov=app --cov-report=term-missing --cov-report=xml --cov-fail-under=80

test-integration: ## Live tests against the real GitHub API and a running service
	RUN_INTEGRATION=1 $(PY) -m pytest tests/integration -v

test-webhook: ## Live tests including a real webhook delivery (needs a tunnel)
	RUN_INTEGRATION=1 RUN_WEBHOOK_INTEGRATION=1 $(PY) -m pytest tests/integration -v

coverage: ## HTML coverage report at htmlcov/index.html
	$(PY) -m pytest tests/unit -q --cov=app --cov-report=html
	@echo "open htmlcov/index.html"

lint: ## Lint the codebase
	$(PY) -m ruff check .

fmt: ## Auto-fix what the linter can
	$(PY) -m ruff check --fix .

spec-lint: ## Validate openapi.yaml against the OpenAPI 3.1 metaschema
	$(PY) -c "import yaml,pathlib; from openapi_spec_validator import validate; \
		validate(yaml.safe_load(pathlib.Path('openapi.yaml').read_text())); \
		print('openapi.yaml is valid OpenAPI 3.1')"

docker-build: ## Build the image
	docker build -t issues-gw:latest .

docker-run: ## Run the image with .env
	docker run --rm -p $(PORT):8080 --env-file .env \
		-v issues-gw-data:/data --name issues-gw issues-gw:latest

compose-up: ## Start the service via compose
	docker compose up --build

compose-tunnel: ## Start the service plus the smee webhook tunnel
	docker compose --profile tunnel up --build

secret: ## Generate a WEBHOOK_SECRET
	@$(PY) -c "import secrets; print(secrets.token_hex(32))"

clean: ## Remove caches, coverage output and the local event DB
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage coverage.xml data
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
