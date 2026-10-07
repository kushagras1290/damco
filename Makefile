.DEFAULT_GOAL := help
WEB := apps/web

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Install Python + web dependencies
	uv sync --all-packages
	cd $(WEB) && pnpm install --frozen-lockfile

up: ## Start the full local stack (Postgres, Temporal, API, worker, web)
	docker compose up --build -d
	@echo "Web http://localhost:3000 | API http://localhost:8000/docs | Temporal UI http://localhost:8233"

down: ## Stop the stack
	docker compose down

seed: ## Seed demo profile + sources (stack must be running)
	docker compose exec api python -m jobpulse.seed /app/seed.yaml

migrate: ## Apply database migrations
	uv run alembic upgrade head

lint: ## Lint Python + web
	uv run ruff check .
	uv run ruff format --check .
	cd $(WEB) && pnpm lint

typecheck: ## Strict type checks
	uv run mypy packages/python/src apps/api/src apps/worker/src
	cd $(WEB) && pnpm typecheck

test: ## Unit + integration + workflow tests (integration needs Docker)
	uv run pytest --cov --cov-report=term-missing
	cd $(WEB) && pnpm test

e2e: ## Playwright smoke tests against the running stack
	cd $(WEB) && pnpm exec playwright test

audit: ## Dependency vulnerability scans
	uv export --frozen --all-packages --no-emit-workspace --format requirements.txt -o .audit-requirements.txt
	uv run pip-audit -r .audit-requirements.txt --disable-pip --strict
	rm -f .audit-requirements.txt
	cd $(WEB) && pnpm audit --prod

check: lint typecheck test ## Everything CI runs before merge

.PHONY: help install up down seed migrate lint typecheck test e2e audit check
