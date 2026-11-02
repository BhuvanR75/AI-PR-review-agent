# Convenience targets. `make` is not installed everywhere (it is absent on the
# Windows box this was built on) -- dev.sh provides the same targets in bash.
# Both are thin wrappers over the CLI: `uv run pr-review <command>`.

.PHONY: help up down logs psql migrate reset-db index-local api worker console \
        simulate check test test-integration lint typecheck fmt sync

help:
	@echo "up/down/logs   - infra (Postgres + Redis via docker compose)"
	@echo "migrate        - apply SQL migrations (capability-probed)"
	@echo "reset-db       - drop and recreate the public schema, then migrate"
	@echo "index-local    - chunk+embed a repo into code_chunks (REPO=path, NAME=name)"
	@echo "api            - run the FastAPI ingress on :8080"
	@echo "worker         - run the ARQ worker"
	@echo "console        - run the Next.js approval console on :3000"
	@echo "simulate       - sign and post a fake GitHub webhook to the local API"
	@echo "check          - lint + typecheck + unit tests (offline, no secrets)"
	@echo "test-integration - tests that need the compose stack"

up:
	docker compose up -d
	@echo "waiting for postgres..."; sleep 3

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

psql:
	docker compose exec postgres psql -U postgres -d prreview

migrate:
	uv run pr-review migrate

reset-db:
	uv run pr-review migrate --reset

index-local:
	uv run pr-review index --path "$(REPO)" --name "$(NAME)"

api:
	uv run pr-review api --reload

worker:
	uv run pr-review worker

console:
	cd frontend && npm run dev

simulate:
	uv run pr-review simulate-webhook

sync:
	uv sync

lint:
	uv run ruff check backend scripts tests

fmt:
	uv run ruff format backend scripts tests

typecheck:
	uv run mypy

test:
	uv run pytest -q -m "not integration"

test-integration:
	uv run pytest -q -m integration

check: lint typecheck test
