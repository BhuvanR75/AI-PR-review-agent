"use client";

import { useState } from "react";
import type { ReactNode } from "react";

import { Banner, Panel } from "@/components/ui";
import { api } from "@/lib/api";
import type { Finding, HitlActionResponse } from "@/lib/types";

/**
 * The decision surface.
 *
 * Three things are deliberate here. Approving posts a COMMENT review and never an
 * approval of the change, which the copy states so a reviewer is not surprised by it.
 * The actor field is required, because an audit trail that says "approved" without
 * saying by whom is not an audit trail. And the finding selection is explicit -- the
 * backend records the excluded ones as feedback, which is the calibration data the
 * confidence gate will eventually be tuned on.
 */
export function ReviewActions({
  reviewId,
  findings,
  findingIds,
  decidable,
  onDone,
}: {
  reviewId: string;
  findings: Finding[];
  findingIds: string[];
  decidable: boolean;
  onDone: () => void;
}): ReactNode {
  const [actor, setActor] = useState("reviewer@local");
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [selected, setSelected] = useState<Set<number>>(() => new Set(findings.map((_, i) => i)));
  const [busy, setBusy] = useState<null | "approve" | "reject" | "rerun">(null);
  const [result, setResult] = useState<HitlActionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(kind: "approve" | "reject" | "rerun"): Promise<void> {
    setBusy(kind);
    setError(null);
    setResult(null);
    try {
      let outcome: HitlActionResponse;
      if (kind === "approve") {
        const all = selected.size === findings.length;
        outcome = await api.approve(reviewId, {
          actor,
          note: note || null,
          // Sending every id is the same as sending none, so this stays `null` when
          // nothing was excluded -- fewer moving parts on the common path.
          finding_ids: all ? null : [...selected].sort((a, b) => a - b).map((i) => findingIds[i]),
        });
      } else if (kind === "reject") {
        if (!reason.trim()) {
          throw new Error("A reason is required: the rejection is what the learning loop reads.");
        }
        outcome = await api.reject(reviewId, { actor, reason });
      } else {
        outcome = await api.rerun(reviewId, { actor, note: note || null, agents: null });
      }
      setResult(outcome);
      onDone();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Panel
      title="Your decision"
      subtitle="Approving posts a COMMENT review to the pull request. Rejecting posts your reason as a comment on the pull request and closes it. Neither approves or blocks the change itself on GitHub — that stays a human's call."
    >
      {error ? <Banner kind="error">{error}</Banner> : null}
      {result ? (
        <Banner kind={result.status === "posted" ? "ok" : "warn"}>
          {result.message}
          {result.github_review_url ? (
            <>
              {" "}
              <a href={result.github_review_url}>Open the posted review</a>
            </>
          ) : null}
        </Banner>
      ) : null}

      {!decidable ? (
        <p className="muted">
          This review is no longer awaiting a decision, so it cannot be approved or rejected.
          {findings.length > 0 ? " Re-run it to get a fresh review of the current commit." : ""}
        </p>
      ) : null}

      <label className="field">
        Acting as (recorded on the audit trail)
        <input type="text" value={actor} onChange={(e) => setActor(e.target.value)} />
      </label>

      <details>
        <summary className="muted" style={{ cursor: "pointer", fontSize: 13 }}>
          Choose which findings to post ({selected.size} of {findings.length})
        </summary>
        <div style={{ marginTop: 10 }}>
          {findings.map((finding, index) => (
            <label key={`${finding.file_path}:${finding.line_start}:${index}`} style={{ display: "block" }}>
              <input
                type="checkbox"
                checked={selected.has(index)}
                onChange={(e) => {
                  const next = new Set(selected);
                  if (e.target.checked) next.add(index);
                  else next.delete(index);
                  setSelected(next);
                }}
              />{" "}
              <span className="mono">
                {finding.severity} {finding.file_path}
                {finding.line_start ? `:${finding.line_start}` : ""}
              </span>{" "}
              — {finding.summary}
            </label>
          ))}
          <p className="muted" style={{ fontSize: 13 }}>
            Excluded findings are recorded as feedback against the pass that raised them.
          </p>
        </div>
      </details>

      <label className="field" style={{ marginTop: 12 }}>
        Note (optional)
        <input type="text" value={note} onChange={(e) => setNote(e.target.value)} />
      </label>

      <label className="field" style={{ marginTop: 12 }}>
        Reason (required to reject — posted to the pull request when it is closed)
        <input type="text" value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>

      <div className="row">
        <div style={{ display: "flex", gap: 10 }}>
          <button
            className="primary"
            disabled={!decidable || busy !== null || selected.size === 0}
            onClick={() => void run("approve")}
          >
            {busy === "approve" ? "Posting…" : "Approve and post"}
          </button>
          <button
            className="danger"
            disabled={!decidable || busy !== null || !reason.trim()}
            onClick={() => void run("reject")}
          >
            {busy === "reject" ? "Rejecting…" : "Reject and close PR"}
          </button>
          <button disabled={busy !== null} onClick={() => void run("rerun")}>
            {busy === "rerun" ? "Re-running…" : "Re-run all passes"}
          </button>
        </div>
      </div>
    </Panel>
  );
}
