#!/usr/bin/env bash
# dev.sh -- the same targets as the Makefile, for machines without `make`
# (this project was scaffolded on a Windows box where make is absent).
#
#   ./dev.sh run | up | migrate | api | worker | check | test | ...
set -euo pipefail

cmd="${1:-help}"
shift || true

run_all() {
  docker compose up -d --wait
  uv run pr-review migrate

  cleanup() {
    trap - SIGINT SIGTERM EXIT
    jobs -pr | xargs -r kill 2>/dev/null || true
  }
  trap cleanup SIGINT SIGTERM EXIT

  uv run pr-review api --reload &
  uv run pr-review worker &
  ( cd frontend && npm run dev ) &

  wait
}

case "$cmd" in
  run)           run_all ;;
  up)            docker compose up -d ;;
  down)          docker compose down ;;
  logs)          docker compose logs -f --tail=100 ;;
  psql)          docker compose exec postgres psql -U postgres -d prreview ;;
  migrate)       uv run pr-review migrate "$@" ;;
  reset-db)      uv run pr-review migrate --reset ;;
  index-local)   uv run pr-review index --path "${REPO:?set REPO=path}" --name "${NAME:-local}" ;;
  api)           uv run pr-review api --reload ;;
  worker)        uv run pr-review worker ;;
  console)       ( cd frontend && npm run dev ) ;;
  simulate)      uv run pr-review simulate-webhook "$@" ;;
  sync)          uv sync ;;
  lint)          uv run ruff check backend scripts tests ;;
  fmt)           uv run ruff format backend scripts tests ;;
  typecheck)     uv run mypy ;;
  test)          uv run pytest -q -m "not integration" ;;
  test-integration) uv run pytest -q -m integration ;;
  check)         uv run ruff check backend scripts tests && uv run mypy && uv run pytest -q -m "not integration" ;;
  help|*)        sed -n '2,12p' "$0" ;;
esac
