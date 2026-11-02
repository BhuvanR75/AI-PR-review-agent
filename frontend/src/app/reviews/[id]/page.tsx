"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef } from "react";

import { DiffView } from "@/components/diff-view";
import { ReviewActions } from "@/components/review-actions";
import { TraceView } from "@/components/trace-view";
import { Banner, Confidence, Panel, SeverityPill, StatusPill } from "@/components/ui";
import { api } from "@/lib/api";
import { SEVERITY_ORDER, money, relativeTime } from "@/lib/format";
import { useAsync } from "@/lib/hooks";
import type { Explanation, ReviewDetail, TraceResponse } from "@/lib/types";

export default function ReviewPage() {
  const params = useParams<{ id: string }>();
  const reviewId = params.id;

  const review = useAsync<ReviewDetail>(() => api.getReview(reviewId), [reviewId]);
  const trace = useAsync<TraceResponse>(() => api.getTrace(reviewId), [reviewId]);
  const audit = useAsync<{ explanations: Explanation[] }>(
    () => api.getExplanation(reviewId),
    [reviewId],
  );

  function reloadAll(): void {
    review.reload();
    trace.reload();
    audit.reload();
  }

  // While the review is queued or running, poll until the findings land. This is
  // what makes "press Review → watch it work → approve" one continuous motion:
  // the page walks itself from queued to pending_approval without a manual refresh.
  const status = review.data?.status;
  const active = status === "queued" || status === "running";
  const wasActive = useRef(false);
  useEffect(() => {
    if (active) {
      wasActive.current = true;
      const timer = setInterval(reloadAll, 3000);
      return () => clearInterval(timer);
    }
    // One final refresh on the transition out of active, so findings, trace and
    // provenance are never a step behind the status change.
    if (wasActive.current) {
      wasActive.current = false;
      reloadAll();
    }
    return undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, reviewId]);

  const detail = review.data;
  const decidable =
    detail?.status === "pending_approval" && (detail.hitl_state === "awaiting" || detail.hitl_state === null);

  if (review.error) {
    return (
      <Panel title="Review">
        <Banner kind="error">{review.error}</Banner>
        <Link href="/">← Back to the queue</Link>
      </Panel>
    );
  }

  if (!detail) {
    return <p className="muted">Loading review…</p>;
  }

  const incomplete = detail.errors.filter((e) => !e.includes("not among the diff"));

  return (
    <>
      <Panel
        title={`${detail.repo} #${detail.pr_number}`}
        subtitle={`${detail.title ?? "(no title)"} · by ${detail.author ?? "unknown"} · head ${detail.head_sha.slice(0, 10)} · ${relativeTime(detail.created_at)}`}
      >
        <div className="row">
          <div className="row" style={{ gap: 10 }}>
            <StatusPill status={detail.status} />
            {detail.hitl_state ? <span className="tag">hitl: {detail.hitl_state}</span> : null}
            {detail.decision ? <span className="tag">gate: {detail.decision}</span> : null}
          </div>
          <div className="row" style={{ gap: 18 }}>
            <span className="muted">
              confidence <Confidence value={detail.overall_confidence} />
            </span>
            <span className="mono muted">{money(detail.cost.usd)}</span>
            <span className="muted">
              {detail.cost.llm_calls} call(s), {detail.cost.tokens_in}→{detail.cost.tokens_out} tok
            </span>
            <button onClick={reloadAll}>Refresh</button>
          </div>
        </div>
        {detail.github_review_url ? (
          <p>
            <a href={detail.github_review_url}>The posted review on GitHub</a>
          </p>
        ) : null}
        {detail.hitl_notes ? <p className="muted">Decision notes: {detail.hitl_notes}</p> : null}
      </Panel>

      {incomplete.length > 0 ? (
        <Banner kind="warn">
          <strong>{incomplete.length} warning(s) on this review.</strong> A pass that did not
          complete leaves part of the diff unverified — treat an empty category as unexamined
          rather than clean.
          <ul>
            {incomplete.slice(0, 8).map((warning) => (
              <li key={warning} className="mono">
                {warning}
              </li>
            ))}
          </ul>
        </Banner>
      ) : null}

      <ReviewActions
        reviewId={detail.id}
        findings={detail.findings}
        findingIds={detail.finding_ids}
        decidable={decidable}
        onDone={reloadAll}
      />

      {active ? (
        <Panel title="Review in progress" subtitle="This page updates itself every few seconds.">
          <p className="muted" style={{ margin: 0 }}>
            The four passes are running over the diff. Findings appear here as soon as
            the aggregator has merged and scored them.
          </p>
        </Panel>
      ) : null}

      {detail.diff ? (
        <Panel
          title="Changed code"
          subtitle="The diff as it was reviewed. Findings are anchored on the lines they name; added lines are green, removed red."
        >
          <DiffView diff={detail.diff} findings={detail.findings} />
        </Panel>
      ) : (
      <Panel
        title={`Findings (${detail.findings.length})`}
        subtitle="Each finding carries the reason it was raised, how sure the pass was, and whether another pass agreed."
      >
        {detail.findings.length === 0 ? (
          <p className="muted">
            No findings. Check the warnings above before reading that as a clean diff.
          </p>
        ) : null}
        {SEVERITY_ORDER.map((severity) => {
          const group = detail.findings.filter((f) => f.severity === severity);
          if (group.length === 0) return null;
          return (
            <section key={severity}>
              <h3 style={{ fontSize: 14, marginBottom: 6 }}>
                <SeverityPill severity={severity} /> {group.length}
              </h3>
              {group.map((finding, index) => (
                <article
                  key={`${finding.file_path}:${finding.line_start}:${finding.category}:${index}`}
                  className="finding"
                  style={{ borderLeftColor: "var(--border)" }}
                >
                  <div className="row">
                    <strong>{finding.summary}</strong>
                    <span className="row" style={{ gap: 8 }}>
                      <span className="tag">{finding.agent_type}</span>
                      <span className="tag">{finding.category}</span>
                      <span className="mono muted">
                        {finding.file_path}
                        {finding.line_start ? `:${finding.line_start}` : ""}
                      </span>
                      <span className="muted">
                        conf <Confidence value={finding.confidence} />
                      </span>
                    </span>
                  </div>
                  <p style={{ margin: "6px 0" }}>{finding.rationale}</p>
                  {finding.suggestion ? (
                    <p className="muted" style={{ margin: "6px 0" }}>
                      <strong>Suggestion:</strong> {finding.suggestion}
                    </p>
                  ) : null}
                  {finding.agreed_by.length > 0 ? (
                    <p className="muted" style={{ margin: 0 }}>
                      Also raised by: {finding.agreed_by.join(", ")}
                    </p>
                  ) : null}
                  {finding.incomplete ? (
                    <p className="muted" style={{ margin: 0 }}>
                      Marked incomplete: the pass could not fully verify this claim.
                    </p>
                  ) : null}
                </article>
              ))}
            </section>
          );
        })}
      </Panel>
      )}

      <Panel
        title="Provenance"
        subtitle="Which model and which prompt version produced each finding — the reason a finding can be disputed rather than merely doubted."
      >
        {audit.error ? <Banner kind="error">{audit.error}</Banner> : null}
        <table>
          <thead>
            <tr>
              <th>Finding</th>
              <th>Model</th>
              <th>Prompt</th>
              <th className="num">Cost</th>
              <th>Reproducible</th>
            </tr>
          </thead>
          <tbody>
            {audit.data?.explanations.map((item) => (
              <tr key={item.locator}>
                <td className="mono">{item.locator}</td>
                <td className="mono">{item.model ?? "unknown"}</td>
                <td className="mono">
                  {item.prompt_version ?? "-"} · {item.prompt_hash ?? "-"}
                </td>
                <td className="num mono">{money(item.cost_usd)}</td>
                <td>
                  {item.reproducible ? "yes" : item.caveat ?? "partial"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      {trace.error ? <Banner kind="error">{trace.error}</Banner> : null}
      <TraceView events={trace.data?.events ?? []} />
    </>
  );
}
