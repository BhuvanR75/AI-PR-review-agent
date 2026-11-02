"""The operator CLI.

Deliberately capable of running the entire loop on its own -- list the queue, inspect a
review, approve it, reject it, re-run it -- so the approval workflow is usable and
testable before the Next.js console exists, and so there is a fallback surface if the
console is down. Both call the same `HitlService`, so they cannot drift.

    uv run pr-review migrate
    uv run pr-review index --path . --name local/repo
    uv run pr-review simulate-webhook
    uv run pr-review queue
    uv run pr-review show <review_id>
    uv run pr-review approve <review_id> --actor you
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
from typing import Any

import httpx

from backend.bootstrap import AppContext, build_context, shutdown_context
from backend.core.exceptions import (
    InvalidReviewState,
    ReviewAgentError,
    ReviewNotFound,
    ReviewSuperseded,
)
from backend.models.enums import AgentType, Severity
from backend.settings import get_settings

SEVERITY_ORDER = [
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
]

DEFAULT_API = "http://127.0.0.1:8080"


# --- formatting --------------------------------------------------------------
def _rule(char: str = "-", width: int = 78) -> str:
    return char * width


def _severity_mark(severity: Severity) -> str:
    return {
        Severity.CRITICAL: "!!",
        Severity.HIGH: "! ",
        Severity.MEDIUM: "~ ",
        Severity.LOW: "- ",
        Severity.INFO: ". ",
    }[severity]


def _print_review(review: Any, stored: list[Any], hitl_state: Any) -> None:
    print(_rule("="))
    print(f"{review.repo} #{review.pr_number}  [{review.status}]")
    print(f"head {review.head_sha[:12]}   confidence {review.overall_confidence}")
    print(f"hitl: {hitl_state.value if hitl_state else 'none'}   decision: {review.decision}")
    if review.title:
        print(f"title: {review.title}")
    if review.github_review_url:
        print(f"posted: {review.github_review_url}")
    print(_rule())

    if not stored:
        print("no findings")
    ordered = sorted(
        stored,
        key=lambda s: (
            SEVERITY_ORDER.index(s.finding.severity),
            s.finding.file_path,
            s.finding.line_start or 0,
        ),
    )
    for item in ordered:
        finding = item.finding
        print(f"{_severity_mark(finding.severity)} {finding.severity.value:<8} {finding.locator}")
        print(f"   {finding.summary}")
        print(
            f"   agent={finding.agent_type.value} category={finding.category.value} "
            f"confidence={finding.confidence:.2f}"
        )
        print(f"   why: {finding.rationale[:220]}")
        if finding.suggestion:
            print(f"   fix: {finding.suggestion[:220]}")
        if item.id:
            print(f"   id:  {item.id}")
        print()
    print(_rule("="))


# --- commands ----------------------------------------------------------------
def cmd_migrate(args: argparse.Namespace) -> int:
    from scripts.migrate import main as migrate_main

    argv: list[str] = []
    if args.reset:
        argv.append("--reset")
    if args.verify:
        argv.append("--verify")
    return migrate_main(argv)


def cmd_connect(args: argparse.Namespace) -> int:
    """Store a GitHub token as the active connection (validated first)."""

    from backend.integrations.connection_store import ConnectionStore, token_fingerprint
    from backend.integrations.github_client import LiveGitHubClient
    from backend.settings import get_settings as _settings

    token = (args.token or "").strip() or input("GitHub token: ").strip()
    if len(token) < 8:
        print("that token looks too short to be a GitHub token")
        return 2
    settings = _settings()
    try:
        check = asyncio.run(LiveGitHubClient(settings, token=token).validate())
    except Exception as exc:  # noqa: BLE001 - report, don't traceback
        print(f"GitHub rejected the token: {exc}")
        return 2
    store = ConnectionStore(settings.database_url)
    asyncio.run(
        store.save(
            token=token,
            user_login=check.login,
            scopes=check.scopes,
            api_base=settings.github_api_base,
            connected_by="cli",
        )
    )
    print(f"connected as {check.login} (token {token_fingerprint(token)})")
    print("the console's repo and PR listing now use this token -- no restart needed")
    if not check.scopes:
        print("note: fine-grained tokens report no scopes; ensure Contents: read is granted")
    return 0


def cmd_whoami(_args: argparse.Namespace) -> int:
    from backend.integrations.connection_store import ConnectionStore
    from backend.settings import get_settings as _settings

    async def _run() -> str:
        store = ConnectionStore(_settings().database_url)
        public = await store.describe()
        return public.user_login if public else "(not connected)"

    print(asyncio.run(_run()))
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    return asyncio.run(_index(args))


async def _index(args: argparse.Namespace) -> int:
    from pathlib import Path

    from backend.database.pg import create_pool
    from backend.memory.code_memory import CodeMemory
    from backend.memory.embedder import get_embedder
    from backend.memory.ingestion import IngestionPipeline
    from backend.settings import get_settings as _settings

    settings = _settings()
    pool = await create_pool(settings.database_url)
    try:
        memory = CodeMemory(pool, get_embedder(settings), settings)
        pipeline = IngestionPipeline(memory)
        root = await asyncio.to_thread(Path(args.path).resolve)
        report = await pipeline.index_repo(root, args.name, force=args.force)
        print(report.summary())
        for error in report.errors[:20]:
            print(f"  error: {error}")
        stats = await memory.stats(args.name)
        print(
            f"memory: {stats.chunks} chunk(s) across {stats.files} file(s), "
            f"{stats.symbols} symbol(s)"
        )
        return 0
    finally:
        from backend.database.pg import close_pool

        await close_pool()


def cmd_api(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "backend.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,
    )
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from arq.worker import run_worker

    from backend.job_queue.arq_worker import WorkerSettings

    run_worker(WorkerSettings)  # type: ignore[arg-type]
    return 0


def cmd_queue(_args: argparse.Namespace) -> int:
    return asyncio.run(_queue())


async def _queue() -> int:
    from backend.hitl.queue import queue_depth
    from backend.job_queue.queue import queue_health
    from backend.settings import get_settings as _settings

    settings = _settings()
    depth = await queue_depth()
    health = await queue_health(settings)
    print(f"HITL_MODE={settings.hitl_mode}  token={'set' if settings.api_token else 'EMPTY'}")
    print(json.dumps(health, indent=2))
    oldest = (
        "none" if depth.oldest_pending_seconds is None else f"{depth.oldest_pending_seconds:.0f}s"
    )
    print(
        f"queued={depth.queued} running={depth.running} "
        f"pending_approval={depth.pending_approval} oldest_pending={oldest}"
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    return asyncio.run(_show(args))


async def _show(args: argparse.Namespace) -> int:
    context = await _context()
    try:
        review, stored, state = await context.hitl.get_review_and_findings(args.review_id)
        _print_review(review, stored, state)
        if args.trace:
            from backend.observability.events import read_trace

            for event in await read_trace(args.review_id):
                print(
                    f"{event.ts:%H:%M:%S} {str(event.agent):<11} {str(event.event_type):<11} "
                    f"{'' if event.latency_ms is None else str(event.latency_ms) + 'ms':<8} "
                    f"{(event.outcome or ''):<10} "
                    f"{('$' + format(event.cost_usd, '.4f')) if event.cost_usd else ''}"
                )
        return 0
    except ReviewNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await shutdown_context(context)


def cmd_approve(args: argparse.Namespace) -> int:
    return asyncio.run(_decide(args, "approve"))


def cmd_reject(args: argparse.Namespace) -> int:
    return asyncio.run(_decide(args, "reject"))


def cmd_rerun(args: argparse.Namespace) -> int:
    return asyncio.run(_decide(args, "rerun"))


async def _decide(args: argparse.Namespace, action: str) -> int:
    context = await _context()
    try:
        if action == "approve":
            only = args.only.split(",") if args.only else None
            outcome = await context.hitl.approve(
                args.review_id, actor=args.actor, note=args.note, finding_ids=only
            )
        elif action == "reject":
            outcome = await context.hitl.reject(
                args.review_id, actor=args.actor, reason=args.reason
            )
        else:
            agents = (
                [AgentType(a.strip()) for a in args.agents.split(",") if a.strip()]
                if args.agents
                else None
            )
            outcome = await context.hitl.rerun(
                args.review_id, actor=args.actor, note=args.note, agents=agents
            )

        print(f"{action}: {outcome.status.value} ({outcome.hitl_state.value})")
        print(outcome.message)
        if outcome.github_review_url:
            print(f"posted: {outcome.github_review_url}")
        return 0
    except (ReviewNotFound, ReviewSuperseded, InvalidReviewState) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ReviewAgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await shutdown_context(context)


def cmd_explain(args: argparse.Namespace) -> int:
    return asyncio.run(_explain(args))


async def _explain(args: argparse.Namespace) -> int:
    from backend.observability.audit import explain_review
    from backend.observability.events import read_trace

    context = await _context()
    try:
        _review, stored, _state = await context.hitl.get_review_and_findings(args.review_id)
        events = await read_trace(args.review_id)
        for explanation in explain_review([s.finding for s in stored], events):
            print(_rule("="))
            print(f"{explanation.finding.locator}  ({explanation.finding.severity.value})")
            print(
                f"model={explanation.model} prompt={explanation.prompt_version}"
                f"@{explanation.prompt_hash}"
            )
            print(f"cost=${explanation.cost_usd:.4f} latency={explanation.latency_ms}ms")
            for line in explanation.evidence:
                print(f"  evidence: {line}")
            if explanation.caveat:
                print(f"  CAVEAT: {explanation.caveat}")
        return 0
    except ReviewNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await shutdown_context(context)


def cmd_simulate(args: argparse.Namespace) -> int:
    return asyncio.run(_simulate(args))


async def _simulate(args: argparse.Namespace) -> int:
    """Sign and POST a webhook to a running API, exactly as GitHub would."""
    from backend.webhook_receiver.validator import compute_signature

    settings = get_settings()
    payload = {
        "action": args.action,
        "number": args.pr,
        "repository": {"full_name": args.repo},
        "pull_request": {
            "number": args.pr,
            "title": "Simulated pull request",
            "draft": False,
            "user": {"login": "simulator"},
            "head": {"sha": args.sha},
            "base": {"sha": "0" * 40},
        },
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": args.delivery,
        "X-Hub-Signature-256": compute_signature(settings.github_webhook_secret, body),
    }
    url = f"{args.api.rstrip('/')}/webhooks/github"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, content=body, headers=headers)
    print(f"POST {url} -> {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2))
    except Exception:  # noqa: BLE001
        print(response.text)
    return 0 if response.status_code < 400 else 1


def cmd_alerts(_args: argparse.Namespace) -> int:
    return asyncio.run(_alerts())


async def _alerts() -> int:
    from backend.observability.alerting import collect_alerts

    gathered = await collect_alerts()
    if not gathered:
        print("no alerts")
        return 0
    for alert in gathered:
        print(f"[{alert.severity.value}] {alert.kind}: {alert.message}")
        print(f"   {json.dumps(alert.context)}")
    return 0


def cmd_economics(args: argparse.Namespace) -> int:
    return asyncio.run(_economics(args))


async def _economics(args: argparse.Namespace) -> int:
    context = await _context()
    try:
        snapshot = await context.costs.snapshot(hours=args.hours)
        print(
            f"today: ${snapshot.daily_cost_usd:.4f} of ${snapshot.budget_usd:.2f} "
            f"({snapshot.utilisation:.1%}) remaining ${snapshot.remaining_usd:.4f}"
        )
        print(_rule())
        print(f"{'agent':<12}{'calls':>7}{'cost':>11}{'p95(ms)':>10}{'reject%':>9}  source")
        for health in snapshot.by_agent:
            p95 = health.p95_latency_ms or 0
            print(
                f"{health.agent:<12}{health.llm_calls:>7}{health.cost_usd:>11.4f}"
                f"{p95:>10.0f}{health.rejection_rate:>8.0%}  {health.source}"
            )
        print(_rule())
        for row in await context.costs.recent_pr_costs(hours=args.hours):
            print(f"{row['review_id']}  ${row['cost_usd']:.4f}  {row['llm_calls']} call(s)")
        return 0
    finally:
        await shutdown_context(context)


# --- wiring ------------------------------------------------------------------
async def _context() -> AppContext:
    """The same context the API and worker build, so the CLI cannot drift from them."""
    return await build_context()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr-review", description="Operate the AI PR review agent."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="apply schema migrations")
    p.add_argument("--reset", action="store_true", help="drop and rebuild the schema")
    p.add_argument("--verify", action="store_true", help="print the resulting schema")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("connect", help="store a GitHub token as the active connection")
    p.add_argument(
        "--token", help="the token (omit to be prompted, so it stays out of shell history)"
    )
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("whoami", help="show the active GitHub connection")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("index", help="chunk and embed a repository into code memory")
    p.add_argument("--path", required=True, help="path to the repository root")
    p.add_argument("--name", required=True, help="repo name to index under, e.g. owner/name")
    p.add_argument("--force", action="store_true", help="re-embed unchanged files too")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("api", help="run the FastAPI ingress")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_api)

    p = sub.add_parser("worker", help="run the ARQ worker")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("queue", help="show queue depth and mode")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("show", help="show one review and its findings")
    p.add_argument("review_id")
    p.add_argument("--trace", action="store_true", help="also print the event spine")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve", help="approve a pending review and post it")
    p.add_argument("review_id")
    p.add_argument("--actor", default="cli")
    p.add_argument("--note", default=None)
    p.add_argument("--only", default=None, help="comma-separated finding ids to post")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="reject a pending review")
    p.add_argument("review_id")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("rerun", help="re-run a review, optionally with a subset of passes")
    p.add_argument("review_id")
    p.add_argument("--actor", default="cli")
    p.add_argument("--note", default=None)
    p.add_argument("--agents", default=None, help="comma-separated: security,quality,tests,docs")
    p.set_defaults(func=cmd_rerun)

    p = sub.add_parser("explain", help="show per-finding provenance")
    p.add_argument("review_id")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("simulate-webhook", help="sign and POST a webhook to a running API")
    p.add_argument("--repo", default="acme/shop")
    p.add_argument("--pr", type=int, default=42)
    p.add_argument("--sha", default="9f2c1b7e4d5a6c8b0e1f2a3b4c5d6e7f8091a2b3")
    p.add_argument("--action", default="opened")
    p.add_argument("--delivery", default="simulated-delivery-1")
    p.add_argument("--api", default=DEFAULT_API)
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("alerts", help="run the alert rules once")
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("economics", help="cost and health report")
    p.add_argument("--hours", type=int, default=24)
    p.set_defaults(func=cmd_economics)

    return parser


#: Exception types that mean "the infrastructure is not up" rather than "the command is
#: wrong". A traceback for a stopped container is a bad first impression and it buries
#: the one instruction that actually helps.
_INFRASTRUCTURE_ERRORS = (
    ConnectionRefusedError,
    ConnectionError,
    TimeoutError,
    socket.gaierror,
)


def _unreachable(exc: BaseException) -> bool:
    return isinstance(exc, _INFRASTRUCTURE_ERRORS)


def _report_unreachable(exc: BaseException) -> int:
    """Say what to run. Deliberately does not try to guess the host from the exception
    text: the DSNs are already known, and a mangled address is worse than none."""
    settings = get_settings()
    print(
        f"\nCould not reach the local stack: {type(exc).__name__}: {exc}\n"
        f"  Postgres expects: {settings.database_url}\n"
        f"  Redis expects:    {settings.redis_url}\n\n"
        "Start it first -- Docker Desktop must be running:\n"
        "  docker compose up -d\n"
        "then apply the schema:\n"
        "  uv run pr-review migrate\n",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - one friendly message beats a traceback
        if not _unreachable(exc):
            raise
        return _report_unreachable(exc)


if __name__ == "__main__":
    sys.exit(main())
