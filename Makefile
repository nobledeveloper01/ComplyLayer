.PHONY: help install fmt lint test test-integration cov cov-unit bench guard readme-check doctor security ci up down clean

# Keep every target in step with .github/workflows/ci.yml, so a green local run
# and a green CI run mean the same thing. When they disagree, CI is right and
# this file is stale.

PY := uv run

# Load a local .env if there is one, so `make` and `./scripts/hello-world.sh`
# agree about which Postgres they mean. A developer machine that already runs a
# Postgres on 5432 needs to say so once, in one place.
ifneq (,$(wildcard ./.env))
include .env
export
endif

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
	$(PY) pytest -m "not integration and not benchmark"

test-integration: ## Full suite including anything that needs live services
	$(PY) pytest

cov: ## Full suite with the coverage gate (90%). Needs Postgres and Redis.
	$(PY) pytest -m "not benchmark" --cov=complylayer --cov-report=term-missing

cov-unit: ## Coverage over the docker-free subset only, for a quick local check
	$(PY) pytest -m "not integration and not benchmark" --cov=complylayer --cov-report=term-missing

bench: ## The D1 benchmark — where the latency budget actually goes
	$(PY) pytest -m benchmark -s

guard: ## The eval/exec guard — see ADR-0001
	$(PY) python scripts/no_eval_guard.py

readme-check: ## Fail if the README has fallen behind PHASE
	./scripts/check-readme-phase.sh

demo: ## End to end in one command: a transaction in, a compliance decision out
	@bash scripts/demo.sh

dashboard: ## Run the dashboard against a seeded throwaway database, and hold it open
	@bash scripts/dashboard_demo.sh

doctor: ## Preflight this deployment's silent failure modes
	$(PY) python manage.py complylayer_doctor

# Semgrep runs here as well as in CI, and the rulesets match ci.yml exactly.
# It used to run only in GitHub, so five findings sat on a green local `make ci`
# and nobody saw them until somebody read the workflow log. A gate that only
# runs somewhere else is a gate that surprises you.
security: ## SAST and dependency audit
	$(PY) bandit -q -c pyproject.toml -r complylayer/
	$(PY) semgrep --error --quiet complylayer/ \
		--config p/python --config p/django --config p/secrets \
		--exclude-rule python.django.security.django-no-csrf-token.django-no-csrf-token
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
