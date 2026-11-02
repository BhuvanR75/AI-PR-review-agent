# AI PR Review Agent

Four specialist reasoners review a pull-request diff in parallel, each grounded in
retrieved repository context. Their findings are merged, deduplicated and scored, and
then **a human approves or rejects the review before anything is posted to GitHub**.
Every action along the way is appended to one time-ordered events table that doubles as
the trace viewer, the audit trail and the cost ledger.

The design comes from an architecture study (*Designing an AI Pull-Request Review
Agent*): the derivation is the interesting part, and each module below says which
question it exists to answer. The short version of the thesis:

- A review agent is not a linter with an LLM bolted on. It is a **fan-out of specialists
  over a diff**, because a senior reviewer thinks in separate passes with separate mindsets.
- An ungrounded reviewer is a confident stranger, so every pass reads **retrieved
  codebase context** rather than the diff alone.
- A review you cannot audit is worthless, so **every action is a row on an events spine**.
- Autonomy is earned case by case, so the **human gate is on by default** and posts a
  `COMMENT` review — never an approval, never a block.

```
GitHub webhook ─► HMAC verify ─► idempotency (delivery id) ─► Redis/ARQ queue
                                                                     │
                                                              LangGraph graph
                                                                     │
      prepare ── fetch diff, supersede stale heads, retrieve context once
         │
      dispatch ──┬─ security pass   ─┐
                 ├─ quality pass     │  each: budget → retrieve → guard/redact →
                 ├─ tests pass       │        prompt → LLM → anchor to the diff
                 └─ docs pass       ─┘  each returns structured findings + confidence
         │
      aggregate ── merge, dedupe, near-miss log, overall confidence, persist
         │
      gate ──► human approval queue (default)   [or auto-post when policy allows]
         │
      human approves ─► publish_review ─► GitHub COMMENT review
                                  │
                                  └─► agent_events (trace + audit + cost, one spine)
```

## The human gate, stated plainly

- `HITL_MODE=always` is the default. Every review stops and waits for a person.
- Publication always submits `event: "COMMENT"`. The agent never approves a change and
  never blocks one — that decision stays with a human on GitHub.
- Approval is refused when the commit has moved on: a newer push marks the old review
  `superseded` and closes its queue entry, so a human cannot approve a review of code
  that no longer exists.
- Rejecting records a feedback row **per finding**, which is what makes a per-pass
  rejection rate computable later. Nothing acts on that feedback automatically yet.

## Quickstart: the guided flow

Prerequisites: [`uv`](https://docs.astral.sh/uv/), Docker (for Postgres and Redis),
and Node 20+ for the web console.

```bash
# 1. infrastructure: one Postgres (TimescaleDB + pgvector) and one Redis
docker compose up -d

# 2. configuration, dependencies, schema
cp backend/.env.example .env
uv sync
uv run pr-review migrate            # reports which storage lanes this server can carry

# 3. run the two processes, in two shells
uv run pr-review api --reload       # http://127.0.0.1:8080
uv run pr-review worker             # runs the four passes

# 4. the console
cd frontend && npm install && cp .env.local.example .env.local && npm run dev
# open http://localhost:3000 and click "Review a PR"
```

On Bash, the complete local stack can also be started with one command:

```bash
./dev.sh run
```

It starts Docker infrastructure, applies migrations, runs the API, worker and console,
and stops the host processes together when you press `Ctrl+C`.

Then the flow is four steps on one screen (`/connect`):

1. **Connect GitHub** — paste a personal access token. It is validated against
   GitHub before being stored (classic token with `repo`, or fine-grained with
   `Contents: read`). Connecting takes effect immediately: the running API and
   worker resolve the connection at call time, no restart. (`pr-review connect`
   does the same from a terminal; omit `--token` to be prompted.)
2. **Pick a repository** — the repos the token can see, filterable.
3. **Pick a pull request** — open PRs, newest first. The diff is fetched at
   trigger time and persisted, so the review sees exactly what you were looking at.
4. **Watch it, then decide** — the review page polls while the four passes run,
   then shows the changed code with findings anchored on the exact lines. Approve
   all findings or only the ones you agree with; the review posts to the PR as a
   COMMENT review. Rejecting keeps it off GitHub and records why.

Everything also works **with no token and no network**: without a connection the
console walks the same flow against a built-in demo repository, the LLM and
embedder sit behind provider switches (`LLM_PROVIDER=fake`,
`EMBEDDING_PROVIDER=fake`), and the dry-run GitHub client writes what it *would*
have posted to `.local/outbox/` and says so in the review body. The fake reviewer
is not a fixture that returns canned strings — it applies real rules to the diff
and reports real new-file line numbers, so anchoring, dedup, severity ordering and
the approval gate are all exercised for real.

Switching to the real thing is a configuration change, not a code change:

```bash
LLM_PROVIDER=openai EMBEDDING_PROVIDER=openai OPENAI_API_KEY=sk-... GITHUB_CLIENT_MODE=live ...
```

## The console

Five screens, all reading the same API the CLI does:

| Screen | What it answers |
| --- | --- |
| **Review a PR** (`/connect`) | The guided flow: connect GitHub, pick a repo, pick an open PR, start a review. |
| **Approval queue** (`/`) | What is waiting on me, how bad is it, and what did it cost? |
| **Review** (`/reviews/<id>`) | The changed code with findings anchored on their lines; every finding with its rationale, confidence and agreement; approve all or a subset, reject with a reason, or re-run; the full trace and per-finding provenance. |
| **Economics** (`/economics`) | Daily spend against the budget, p95 latency and rejection rate per pass, alerts, most expensive reviews. |
| **Agents** (`/agents`) | Which passes exist, which model and prompt version each runs, and what each is allowed to do (none of them may post). |

`NEXT_PUBLIC_API_TOKEN` must match the backend's `API_TOKEN`, which means the token is
in the browser bundle. That is acceptable for a local console and unacceptable for a
publicly hosted one — see *Security posture*.

## CLI reference

Every command runs offline except `explain`, `show`, `queue` and `economics`, which read
the database.

| Command | Purpose |
| --- | --- |
| `pr-review migrate [--reset] [--verify]` | Apply SQL migrations with a capability probe; `--reset` rebuilds the schema. |
| `pr-review connect [--token]` | Validate a GitHub token and store it as the active connection (omit `--token` to be prompted). |
| `pr-review whoami` | Show the active GitHub connection. |
| `pr-review index --path <dir> --name <repo>` | Chunk, embed and index a repository into code memory. |
| `pr-review api [--host] [--port] [--reload]` | Run the FastAPI ingress (webhook + API). |
| `pr-review worker` | Run the ARQ worker that consumes review jobs. |
| `pr-review simulate-webhook` | Sign and POST a fake GitHub webhook to a running API. |
| `pr-review queue` | Queue depth, oldest pending age, and the active HITL mode. |
| `pr-review show <id> [--trace]` | One review with its findings, optionally the full events spine. |
| `pr-review approve <id> [--actor] [--note] [--only <ids>]` | Approve and post; `--only` posts a subset. |
| `pr-review reject <id> --reason <text> [--actor]` | Reject, recording per-finding feedback. |
| `pr-review rerun <id> [--agents a,b] [--actor] [--note]` | Re-run all passes or a subset, clearing the old findings first. |
| `pr-review explain <id>` | Per-finding provenance: model, prompt version, prompt hash, cost. |
| `pr-review alerts` | Current alerts derived from the events spine. |
| `pr-review economics [--hours N]` | Spend, per-pass health, and routing advice. |

## One data spine

The study's most consequential decision: memory, truth and time are three *data shapes*,
and one Postgres carries all three. They are separate lanes, not separate databases.

| Lane | Tables | Shape |
| --- | --- | --- |
| Memory (semantic) | `code_chunks`, `repo_file_index` | pgvector embeddings + `tsvector`, hybrid retrieval by reciprocal rank fusion |
| Truth (episodic) | `pr_review_records`, `finding_records`, `dedup_near_misses`, `hitl_reviews`, `hitl_feedback`, `webhook_deliveries` | ordinary relational rows and transactions |
| Time | `agent_events` | append-only, time-ordered; hypertable when TimescaleDB is present |

The payoff is that "what did this PR retrieve, what review did it produce, and which model
calls made it expensive?" is one indexed query — a joined answer, not three systems
stitched together in Python. There is no `UPDATE` or `DELETE` path for `agent_events`
anywhere in the code, which is what lets the same table be both a debugging trace and an
audit record.

`scripts/migrate.py` probes the server and applies what it can actually carry:

| Lane | If present | If absent |
| --- | --- | --- |
| `timescaledb` | `agent_events` becomes a hypertable; `agent_health_1m` and `pr_cost_hourly` continuous aggregates serve the dashboard | same facts read from the raw table with plain SQL over a bounded window |
| `pgvectorscale` | DiskANN index on the embeddings | HNSW index (`003_index_hnsw.sql`), same operator class, same query |

Both absences are correctness-preserving, just slower — and `/api/meta/capabilities`
reports which one you are running. Pointing `DATABASE_URL` at Tiger Cloud is a
connection-string change, because both it and the local `timescaledb-ha` image are
Postgres with the same extensions.

Two deliberate deviations from the study's letter, both in its spirit: the memory client
is `backend/memory/code_memory.py` rather than a `TigerMemoryClient` (what it talks to is
a repository of code chunks, and which Postgres is underneath is a connection string, not
a different class), and p95 latency is computed exactly from the raw spine over a bounded
window instead of from the rollup, because continuous aggregates cannot use ordered-set
aggregates and `percentile_agg` needs a toolkit that is not guaranteed present.

## How it fails

Every external edge is bounded, and every degradation is *slower but correct* rather than
fast and wrong.

| Failure | Defense in the code |
| --- | --- |
| Hallucinated finding | Findings are anchored to files and lines the diff actually touched; anything outside it is dropped with a recorded reason, and an unanchored finding loses its line rather than becoming a silently rejected comment. |
| LLM or GitHub stalls | Per-call timeout, retries with backoff, and a circuit breaker per provider (shared across passes, so an outage opens one circuit instead of being probed four times). |
| A specialist dies | A failed pass contributes an incomplete result and the review continues; the review says how many passes were incomplete rather than implying the diff was clean. |
| Aggregator waits forever | Every node has a timeout and the graph has a recursion limit; the join always receives a value. |
| Redelivered webhook | Idempotency at ingress by delivery id, and one review row per `(repo, pr, head_sha)`. |
| Duplicate findings | Dedup by `(file, anchor line, category)` with agent agreement recorded; near-misses are logged to `dedup_near_misses` so the rule can be tuned on real data. |
| Budget run away | `BudgetGuard` checks the daily spend before every call and hard-blocks; a blocked review still reaches a human, marked incomplete, with no findings fabricated to fill the gap. |
| Silent audit outage | Spine writes never raise, but failures are counted and surfaced by `/readyz`, `/api/diagnostics` and an alert. |

## Tests

```bash
./dev.sh check                 # or: make check  →  ruff + mypy + unit tests
uv run pytest -q -m "not integration"   # 227 tests, no database, no network
uv run pytest -q -m integration         # needs `docker compose up -d`
```

The unit suite runs entirely against the fakes and asserts on behaviour that matters: that
a bare `except` is caught, that a call broken across lines is attributed to its first
line, that dedup keeps the higher severity and records the disagreement, that a
superseded review cannot be published, that budget exhaustion produces an incomplete
review rather than a fabricated one, that redacting a secret does not hide it from the
reviewer, and that every route the console calls is actually registered.

Integration tests skip with an explicit reason when Postgres is unreachable rather than
failing, and they are the ones that check the claims that only a real database can check:
that the vector column is dimensioned as configured, that migrations are idempotent, that
`(id, ts)` is the primary key so the events table stays promotable to a hypertable, and
that the whole loop — webhook to approved-and-posted review — works end to end.

## Layout

```
backend/
  agents/          four specialists + shared pass shape (budget, retrieval, anchoring)
  api/             REST: reviews, HITL decisions, economics, queue, meta, health
  core/            WorkflowEngine interface + shared exceptions  (inward-only)
  database/        SQLAlchemy models/session/repositories, raw asyncpg pool
  economics/       cost ledger queries, BudgetGuard, routing advice
  hitl/            gate policy, approval queue, decisions, feedback, publishing
  integrations/    GitHub client (live + dry-run) and the demo PR
  job_queue/       ARQ queue and worker
  memory/          embedder, hybrid retrieval, code memory, ingestion
  models/          the contracts: Finding, Review, WebhookEvent, enums
  observability/   the events spine, tracing, audit, alerts, logging
  orchestrator/    LangGraph graph, nodes, state, aggregator, serde
  prompts/         versioned, hashed prompt templates per specialist
  reliability/     retry, timeout, circuit breaker, idempotency
  security/        prompt-injection guard, secret redaction, the threat model as data
  tools/           LLM clients, model router, diff parsing, capability scopes
  webhook_receiver/ HMAC validation, payload parsing, routing to the queue
scripts/
  migrate.py       capability-probed migration runner
  migrations/      001_core.sql, then the optional timescale / diskann / hnsw lanes
frontend/          Next.js approval console
```

Dependencies point inward only: `core` depends on nothing, outer modules depend inward,
`observability` is cross-cutting. Deleting any outer module leaves the inner ones
compiling, and `core/workflow_engine.py` is the seam that would let LangGraph be replaced
without touching an agent.

## Security posture, honestly

The threat model lives in code — `backend/security/threat_model.py`, served at
`GET /api/meta/threat-model` — and every entry names the module implementing its control,
with a unit test that fails the build when one of those paths moves. Eight threats,
eight residual risks; the same table is in that module and not duplicated here, so there
is one place to keep honest.

What matters in practice:

- **Untrusted text is treated as data.** Diff and retrieved context are scanned for
  instruction-shaped spans ("ignore your instructions", "mark this as safe", role
  markers, requests for the system prompt) before the prompt is assembled. Flags are
  written to the events spine and surfaced as warnings on the review. The text is *not*
  deleted: a hostile comment is exactly what a human approving the review needs to see.
- **Credentials are redacted before they leave the machine.** A credential-shaped literal
  becomes a marker, so the value never reaches the model provider and never lands in a
  finding's rationale, the database or a GitHub comment — while still being reported,
  because the marker keeps the finding raisable. Worked payloads are in
  `tests/unit/test_security.py`.
- **Capabilities are the real boundary.** Specialists can read and return findings and
  nothing else; `POST_REVIEW` is enforced at the single publication point, whichever code
  path reached it.
- **Auth is a single shared bearer token.** No per-user identity, no roles, no RBAC. The
  `actor` on every decision is a string the caller supplies — enough for a local audit
  trail, not enough for a multi-user deployment.
- **The console's token is in the browser bundle**, so it is a development tool. Put it
  behind a private network or a real identity layer before hosting it.
- **Dry run by default.** `GITHUB_CLIENT_MODE` must be explicitly set to `live` before a
  single byte reaches GitHub.

## Deliberately not built yet

These are deferred, not forgotten, and each is called out where it would live:

- **Confidence-gated auto-post.** The policy exists (`HITL_MODE=confidence_gated`,
  `HITL_CONFIDENCE_THRESHOLD`) and is off by default. It stays off until approval data
  shows the confidence signal is worth trusting — the gate is deferred rather than solved.
- **Calibration.** Findings carry the pass's own confidence, not a calibrated probability,
  and the UI says so.
- **RBAC, sandboxed tool execution, the offline eval harness, drift detection, canary
  prompt releases.** The events spine already carries what each of them needs (per-pass
  cost, rejection rate, prompt hashes, outcomes); the consumers do not exist yet. The
  injection guard detects poisoned input; it does not contain a tool that acts on it,
  because no such tool is reachable to a specialist in the first place.
