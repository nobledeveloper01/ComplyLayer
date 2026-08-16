.PHONY: help install fmt lint test test-integration cov guard readme-check doctor security ci up down clean

# Keep every target in step with .github/workflows/ci.yml, so a green local run
# and a green CI run mean the same thing. When they disagree, CI is right and
# this file is stale.

PY := uv run

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-18s\033[0m %s\n",$$1,$$2}'

install: ## Create the venv and install everything, Python 3.12 included
	uv sync

fmt: ## Format
	$(PY) ruff format .

lint: ## Lint and check formatting
	$(PY) ruff check .
	$(PY) ruff format --check .

test: ## Unit tests, no Postgres or Redis needed
	$(PY) pytest -m "not integration"

test-integration: ## Full suite including anything that needs live services
	$(PY) pytest

cov: ## Unit tests with the coverage gate (90%)
	$(PY) pytest -m "not integration" --cov=complylayer --cov-report=term-missing

guard: ## The eval/exec guard — see ADR-0001
	$(PY) python scripts/no_eval_guard.py

readme-check: ## Fail if the README has fallen behind PHASE
	./scripts/check-readme-phase.sh

doctor: ## Preflight this deployment's silent failure modes
	$(PY) python manage.py complylayer_doctor

security: ## SAST and dependency audit
	$(PY) bandit -q -c pyproject.toml -r complylayer/
	$(PY) pip-audit --skip-editable
	gitleaks detect --no-banner --redact

up: ## Start Postgres and Redis
	docker compose up -d

down: ## Stop them
	docker compose down

ci: guard readme-check lint cov security ## Everything CI runs, in CI's order
	@echo "ci: ok"

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
