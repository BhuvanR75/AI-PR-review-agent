"use client";

import Link from "next/link";

import { Banner, Confidence, Panel, SeverityCounts, StatusPill } from "@/components/ui";
import { api } from "@/lib/api";
import { humanDuration, money, relativeTime } from "@/lib/format";
import { useAsync } from "@/lib/hooks";
import type { QueueDepth, ReviewSummary } from "@/lib/types";

interface QueueView {
  pending: ReviewSummary[];
  active: ReviewSummary[];
  depth: QueueDepth;
}

export default function QueuePage() {
  const { data, error, loading, reload } = useAsync<QueueView>(async () => {
    const [pending, active, depth] = await Promise.all([
      api.listReviews({ status: "pending" }),
      api.listReviews({ status: "active" }),
      api.queueDepth(),
    ]);
    return { pending, active, depth };
  }, []);

  return (
    <>
      <Panel
        title="Approval queue"
        subtitle="Reviews the agent has finished. Approving one posts it to GitHub as a COMMENT review."
      >
        {error ? <Banner kind="error">{error}</Banner> : null}
        <div className="row">
          <div>
            <strong>{data?.depth.pending_approval ?? 0}</strong>{" "}
            <span className="muted">awaiting a human</span>
          </div>
          <div className="muted">
            {data?.depth.queued ?? 0} queued · {data?.depth.running ?? 0} running · oldest{" "}
            {humanDuration(data?.depth.oldest_pending_seconds ?? null)}
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            <Link href="/connect">
              <button className="primary">Review a PR →</button>
            </Link>
            <button onClick={reload} disabled={loading}>
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>
      </Panel>

      <Panel title="Awaiting your decision">
        {loading && !data ? <p className="muted">Loading…</p> : null}
        {data && data.pending.length === 0 ? (
          <p className="muted">Nothing is waiting. The queue is clear.</p>
        ) : null}
        <div className="stack">
          {data?.pending.map((review) => (
            <article key={review.id} className="panel" style={{ marginBottom: 0 }}>
              <div className="row">
                <div>
                  <Link href={`/reviews/${review.id}`}>
                    <strong>
                      {review.repo} #{review.pr_number}
                    </strong>
                  </Link>
                  <div className="muted">
                    {review.title ?? "(no title)"} · by {review.author ?? "unknown"} ·{" "}
                    {relativeTime(review.created_at)}
                  </div>
                </div>
                <div className="row" style={{ gap: 16 }}>
                  <SeverityCounts counts={review.severity_counts} />
                  <span className="muted">
                    confidence <Confidence value={review.overall_confidence} />
                  </span>
                  <span className="mono muted">{money(review.cost.usd)}</span>
                  <Link href={`/reviews/${review.id}`}>Review →</Link>
                </div>
              </div>
            </article>
          ))}
        </div>
      </Panel>

      <Panel title="In flight" subtitle="Queued, running, or already decided.">
        <table>
          <thead>
            <tr>
              <th>Pull request</th>
              <th>Status</th>
              <th>Findings</th>
              <th>Confidence</th>
              <th className="num">Cost</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {data?.active.map((review) => (
              <tr key={review.id}>
                <td>
                  <Link href={`/reviews/${review.id}`}>
                    {review.repo} #{review.pr_number}
                  </Link>
                </td>
                <td>
                  <StatusPill status={review.status} />
                </td>
                <td>
                  <SeverityCounts counts={review.severity_counts} />
                </td>
                <td>
                  <Confidence value={review.overall_confidence} />
                </td>
                <td className="num mono">{money(review.cost.usd)}</td>
                <td className="muted">{relativeTime(review.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.active.length === 0 ? <p className="muted">No reviews yet.</p> : null}
      </Panel>
    </>
  );
}
