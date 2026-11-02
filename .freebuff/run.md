# Run doc — AI PR Review Agent (this workspace = the main checkout)

Two processes run against the Docker stack (Postgres :5433, Redis :6380 via
`docker compose up -d`). Config already lives in place — do not commit it.

## Reproduce the uncommitted artifacts

- `.env` at the repo root — copy from `backend/.env.example` (`cp backend/.env.example .env`)
  and keep the local values (API on port 8080, `API_TOKEN=dev-api-token`).
- `frontend/.env.local` — copy from `frontend/.env.local.example`. It points the
  console at `http://localhost:8080`; adapt the port if the API moves.
- Dependencies:
  - backend: `uv sync`
  - frontend: `cd frontend && npm install`
- Schema: `uv run pr-review migrate` (requires the Docker stack up).

## Run the servers

1. Infrastructure: `docker compose up -d` (healthy in ~1 min on first pull).
2. API: `uv run pr-review api --port 8080` → http://localhost:8080 (`/healthz`).
3. Worker (required — it runs the four review passes): `uv run pr-review worker`.
4. Frontend console: `cd frontend && npm run dev` → http://localhost:3000.

The preview registered for this thread is step 4 only; steps 2–3 are the API and
worker it talks to and must be running for the console to show real data.

Full user flow: open http://localhost:3000/connect → connect a GitHub token
(demo catalogue works without one) → pick repo → pick PR → Review → approve.
