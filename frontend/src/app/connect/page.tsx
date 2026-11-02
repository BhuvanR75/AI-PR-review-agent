"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Banner, Panel } from "@/components/ui";
import { api } from "@/lib/api";
import type {
  ConnectionStatus,
  PullRequestSummary,
  RepoSummary,
  TriggerResponse,
} from "@/lib/types";

/**
 * The guided flow: connect GitHub → pick a repo → pick a PR → review.
 *
 * One screen, four steps, always visible as a stepper so the user knows where they
 * are and what remains. Nothing is hidden behind navigation: the repos appear under
 * the connect step, the PRs under the repo step, and pressing Review navigates
 * straight to the review page, which polls until the findings land.
 */

type StepState = "done" | "current" | "later";

function stepState(step: number, furthest: number): StepState {
  if (step < furthest) return "done";
  if (step === furthest) return "current";
  return "later";
}

function Step({ n, label, state }: { n: number; label: string; state: StepState }) {
  const marker =
    state === "done" ? "✓" : state === "current" ? String(n) : String(n);
  const style: React.CSSProperties =
    state === "done"
      ? { background: "#027a48", color: "#fff" }
      : state === "current"
        ? { background: "#175cd3", color: "#fff" }
        : { background: "var(--border)", color: "var(--muted)" };
  return (
    <span className="row" style={{ justifyContent: "flex-start", gap: 8 }}>
      <span
        className="mono"
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          width: 22,
          height: 22,
          borderRadius: "50%",
          fontSize: 12,
          fontWeight: 700,
          ...style,
        }}
      >
        {marker}
      </span>
      <span style={{ fontWeight: state === "current" ? 650 : 400 }}>{label}</span>
    </span>
  );
}

function Steps({ furthest }: { furthest: number }) {
  return (
    <div className="row" style={{ justifyContent: "flex-start", gap: 24, flexWrap: "wrap" }}>
      <Step n={1} label="Connect GitHub" state={stepState(1, furthest)} />
      <Step n={2} label="Pick a repository" state={stepState(2, furthest)} />
      <Step n={3} label="Pick a pull request" state={stepState(3, furthest)} />
      <Step n={4} label="Review & approve" state={stepState(4, furthest)} />
    </div>
  );
}

export default function ConnectPage() {
  const router = useRouter();

  // --- state ---------------------------------------------------------------
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  const [repos, setRepos] = useState<RepoSummary[] | null>(null);
  const [reposLoading, setReposLoading] = useState(false);
  const [reposError, setReposError] = useState<string | null>(null);
  const [repoFilter, setRepoFilter] = useState("");
  const [selectedRepo, setSelectedRepo] = useState<string | null>(null);

  const [pulls, setPulls] = useState<PullRequestSummary[] | null>(null);
  const [pullsLoading, setPullsLoading] = useState(false);
  const [pullsError, setPullsError] = useState<string | null>(null);

  const [triggering, setTriggering] = useState<number | null>(null);
  const [triggerError, setTriggerError] = useState<string | null>(null);

  // Load connection status on mount. The repos list loads either way: with a
  // connection it is the user's own repositories; without one it is the demo
  // catalogue, so the flow is walkable offline as the copy promises.
  const loadStatus = useCallback(async () => {
    setStatusError(null);
    try {
      const s = await api.connectionStatus();
      setStatus(s);
      setReposLoading(true);
      api
        .listRepos()
        .then((r) => {
          setRepos(r.repos);
          setReposError(null);
        })
        .catch((e: unknown) => setReposError(e instanceof Error ? e.message : String(e)))
        .finally(() => setReposLoading(false));
    } catch (e) {
      setStatusError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  async function connect(): Promise<void> {
    setConnecting(true);
    setConnectError(null);
    try {
      const res = await api.connect(token.trim());
      setToken("");
      setStatus({
        connected: true,
        login: res.login,
        scopes: res.scopes,
        fingerprint: res.fingerprint,
        mode: "live",
        message: res.message,
      });
      // Connected: fetch repos immediately so step 2 fills in.
      setReposLoading(true);
      const r = await api.listRepos();
      setRepos(r.repos);
      setReposLoading(false);
    } catch (e) {
      setConnectError(e instanceof Error ? e.message : String(e));
    } finally {
      setConnecting(false);
    }
  }

  async function disconnect(): Promise<void> {
    setConnecting(true);
    setConnectError(null);
    try {
      await api.disconnect();
      setStatus(null);
      setRepos(null);
      setSelectedRepo(null);
      setPulls(null);
      await loadStatus();
    } catch (e) {
      setConnectError(e instanceof Error ? e.message : String(e));
    } finally {
      setConnecting(false);
    }
  }

  function pickRepo(fullName: string): void {
    setSelectedRepo(fullName);
    setPulls(null);
    setPullsLoading(true);
    setPullsError(null);
    api
      .listPulls(fullName)
      .then((r) => {
        setPulls(r.pulls);
        setPullsError(null);
      })
      .catch((e: unknown) => setPullsError(e instanceof Error ? e.message : String(e)))
      .finally(() => setPullsLoading(false));
  }

  async function review(repo: string, number: number): Promise<void> {
    setTriggering(number);
    setTriggerError(null);
    try {
      const res: TriggerResponse = await api.triggerReview(repo, number);
      // Straight to the review; it polls itself until the findings land.
      router.push(`/reviews/${res.review_id}`);
    } catch (e) {
      setTriggerError(e instanceof Error ? e.message : String(e));
      setTriggering(null);
    }
  }

  const furthest = status?.connected ? 2 : 1;
  const visibleRepos = (repos ?? []).filter((r) =>
    repoFilter.trim()
      ? r.full_name.toLowerCase().includes(repoFilter.trim().toLowerCase())
      : true,
  );

  return (
    <>
      <Panel title="Review a pull request" subtitle="Four steps. Nothing posts to GitHub until you approve it.">
        <Steps furthest={furthest} />
        {statusError ? (
          <Banner kind="error">
            {statusError} — is the API running on {"http://localhost:8080"}?
          </Banner>
        ) : null}
      </Panel>

      {/* Step 1: connect ----------------------------------------------------- */}
      <Panel
        title="1 · GitHub access"
        subtitle={
          status?.connected
            ? `Connected as ${status.login} (token ${status.fingerprint}). Fine-grained tokens need Contents: read; classic needs repo.`
            : "Paste a personal access token. It is validated with GitHub before being stored, and is never shown again."
        }
      >
        {status?.connected ? (
          <div className="row">
            <span className="tag">{status.message}</span>
            {status.scopes.length > 0 ? (
              <span className="muted" style={{ fontSize: 13 }}>
                scopes: {status.scopes.join(", ")}
              </span>
            ) : null}
            <button onClick={() => void disconnect()} disabled={connecting}>
              Disconnect
            </button>
          </div>
        ) : (
          <>
            {connectError ? <Banner kind="error">{connectError}</Banner> : null}
            <div className="row" style={{ justifyContent: "flex-start" }}>
              <input
                type="password"
                placeholder="ghp_… or github_pat_…"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                style={{ maxWidth: 420 }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && token.trim().length >= 8) void connect();
                }}
              />
              <button
                className="primary"
                disabled={connecting || token.trim().length < 8}
                onClick={() => void connect()}
              >
                {connecting ? "Validating…" : "Connect GitHub"}
              </button>
            </div>
            <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
              No token at hand? The demo catalogue below still works — it walks the same
              flow against a built-in sample repository.
            </p>
          </>
        )}
      </Panel>

      {/* Step 2: repos -------------------------------------------------------- */}
      <Panel
        title="2 · Repository"
        subtitle={
          selectedRepo
            ? `Showing open pull requests for ${selectedRepo}.`
            : "Pick the repository whose pull requests you want to review."
        }
      >
        {reposLoading ? <p className="muted">Loading repositories…</p> : null}
        {reposError ? <Banner kind="error">{reposError}</Banner> : null}
        {repos && visibleRepos.length === 0 && !reposLoading ? (
          <p className="muted">No repositories visible to this token.</p>
        ) : null}
        {repos !== null ? (
          <input
            type="text"
            placeholder="Filter repositories…"
            value={repoFilter}
            onChange={(e) => setRepoFilter(e.target.value)}
            style={{ maxWidth: 360, marginBottom: 12 }}
          />
        ) : null}
        <div className="stack">
          {visibleRepos.map((r) => (
            <button
              key={r.full_name}
              onClick={() => pickRepo(r.full_name)}
              style={{
                textAlign: "left",
                borderColor: selectedRepo === r.full_name ? "#175cd3" : undefined,
                borderWidth: selectedRepo === r.full_name ? 2 : 1,
              }}
            >
              <strong className="mono">{r.full_name}</strong>
              {r.private ? <span className="tag" style={{ marginLeft: 8 }}>private</span> : null}
              {r.description ? (
                <div className="muted" style={{ fontSize: 13 }}>
                  {r.description}
                </div>
              ) : null}
            </button>
          ))}
        </div>
      </Panel>

      {/* Step 3: PRs ----------------------------------------------------------- */}
      <Panel
        title="3 · Open pull requests"
        subtitle="Pick a PR to review. The diff is fetched now and reviewed exactly as shown."
      >
        {!selectedRepo ? (
          <p className="muted">Pick a repository above to list its open pull requests.</p>
        ) : pullsLoading ? (
          <p className="muted">Loading pull requests for {selectedRepo}…</p>
        ) : pullsError ? (
          <Banner kind="error">{pullsError}</Banner>
        ) : pulls && pulls.length === 0 ? (
          <p className="muted">No open pull requests in {selectedRepo}.</p>
        ) : null}
        {triggerError ? <Banner kind="error">{triggerError}</Banner> : null}
        <div className="stack">
          {(pulls ?? []).map((p) => (
            <article key={p.number} className="row" style={{ alignItems: "flex-start" }}>
              <div>
                <strong>
                  #{p.number} {p.title}
                </strong>
                <div className="muted" style={{ fontSize: 13 }}>
                  by {p.author} · head {p.head_sha.slice(0, 8)}
                  {p.draft ? " · draft" : ""}
                </div>
              </div>
              <button
                className="primary"
                disabled={triggering !== null}
                onClick={() => void review(p.repo, p.number)}
              >
                {triggering === p.number ? "Starting review…" : "Review this PR"}
              </button>
            </article>
          ))}
        </div>
      </Panel>

      {/* Step 4 is the review page itself */}
      <Panel title="4 · Review & approve" subtitle="What happens after you press the button.">
        <ol className="muted" style={{ margin: 0, paddingLeft: 18, fontSize: 14 }}>
          <li>
            The four passes — security, quality, tests, docs — run over the diff and you
            watch progress live on the review page.
          </li>
          <li>
            Results appear anchored on the changed code, each with its rationale and
            confidence.
          </li>
          <li>
            You approve — all findings or only the ones you agree with — and the review
            posts to the PR. Rejecting keeps it off GitHub and records why.
          </li>
        </ol>
        <p style={{ marginBottom: 0 }}>
          <Link href="/">Or go to the approval queue →</Link>
        </p>
      </Panel>
    </>
  );
}
