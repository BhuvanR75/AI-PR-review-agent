"use client";

import { Banner, Panel } from "@/components/ui";
import { api } from "@/lib/api";
import { money, percent } from "@/lib/format";
import { useAsync } from "@/lib/hooks";
import type { EconomicsSummary, FeedbackSignal } from "@/lib/types";

interface EconomicsView {
  summary: EconomicsSummary;
  feedback: FeedbackSignal[];
  alerts: { kind: string; severity: string; message: string }[];
}

export default function EconomicsPage() {
  const { data, error, loading, reload } = useAsync<EconomicsView>(async () => {
    const [summary, feedback, alerts] = await Promise.all([
      api.economics(24),
      api.feedback(168),
      api.alerts(),
    ]);
    return { summary, feedback: feedback.signals, alerts: alerts.alerts };
  }, []);

  if (error) {
    return (
      <Panel title="Economics">
        <Banner kind="error">{error}</Banner>
      </Panel>
    );
  }

  const summary = data?.summary;

  return (
    <>
      <Panel
        title="Cost and health"
        subtitle="Read from the same events spine as the trace viewer, so these numbers cannot disagree with what a review actually did."
      >
        <div className="row">
          <div className="row" style={{ gap: 26 }}>
            <div>
              <div className="muted">Today</div>
              <div style={{ fontSize: 22, fontWeight: 650 }}>
                {money(summary?.daily_cost_usd ?? 0)}
              </div>
            </div>
            <div>
              <div className="muted">Budget</div>
              <div style={{ fontSize: 22, fontWeight: 650 }}>
                {money(summary?.budget_usd ?? 0)}
              </div>
            </div>
            <div>
              <div className="muted">Used</div>
              <div style={{ fontSize: 22, fontWeight: 650 }}>
                {percent(summary?.utilisation ?? 0)}
              </div>
            </div>
          </div>
          <div className="row" style={{ gap: 12 }}>
            <span className="tag">
              rollups: {summary?.rollups_available ? "continuous aggregates" : "raw spine"}
            </span>
            <button onClick={reload} disabled={loading}>
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>
        {!summary?.rollups_available ? (
          <p className="muted" style={{ marginBottom: 0 }}>
            TimescaleDB is not present, so the rollups are absent and the same answers come from
            plain SQL over the raw events table. Correct, just slower.
          </p>
        ) : null}
      </Panel>

      {data && data.alerts.length > 0 ? (
        <Panel title={`Alerts (${data.alerts.length})`}>
          {data.alerts.map((alert) => (
            <Banner key={`${alert.kind}-${alert.message}`} kind={alert.severity === "critical" ? "error" : "warn"}>
              <strong>{alert.kind}</strong> — {alert.message}
            </Banner>
          ))}
        </Panel>
      ) : null}

      <Panel title="Per pass" subtitle="Cost, latency and how often a human rejected what a pass raised.">
        <table>
          <thead>
            <tr>
              <th>Pass</th>
              <th className="num">Model calls</th>
              <th className="num">Cost</th>
              <th className="num">Avg</th>
              <th className="num">p95</th>
              <th className="num">Rejected</th>
              <th className="num">Rejection rate</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {summary?.by_agent.map((agent) => (
              <tr key={agent.agent}>
                <td>{agent.agent}</td>
                <td className="num">{agent.llm_calls}</td>
                <td className="num mono">{money(agent.cost_usd)}</td>
                <td className="num">{Math.round(agent.avg_latency_ms)}ms</td>
                <td className="num">
                  {agent.p95_latency_ms === null ? "-" : `${Math.round(agent.p95_latency_ms)}ms`}
                </td>
                <td className="num">
                  {agent.rejected}/{agent.decisions}
                </td>
                <td className="num">{percent(agent.rejection_rate)}</td>
                <td className="muted">{agent.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {summary && summary.by_agent.length === 0 ? (
          <p className="muted">No model calls in the last 24 hours.</p>
        ) : null}
      </Panel>

      <Panel
        title="Feedback"
        subtitle="Collected from day one and deliberately not acted on until there is enough of it to mean something."
      >
        <table>
          <thead>
            <tr>
              <th>Pass</th>
              <th className="num">Approved</th>
              <th className="num">Rejected</th>
              <th className="num">Edited out</th>
              <th className="num">Rejection rate</th>
              <th>Recommendation</th>
            </tr>
          </thead>
          <tbody>
            {data?.feedback.map((signal) => (
              <tr key={signal.agent}>
                <td>{signal.agent}</td>
                <td className="num">{signal.approvals}</td>
                <td className="num">{signal.rejections}</td>
                <td className="num">{signal.edits}</td>
                <td className="num">{percent(signal.rejection_rate)}</td>
                <td className={signal.enough_evidence ? "" : "muted"}>{signal.recommendation}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.feedback.length === 0 ? (
          <p className="muted">No decisions recorded yet.</p>
        ) : null}
      </Panel>

      <Panel title="Most expensive reviews" subtitle="Last 24 hours, worst first.">
        <table>
          <thead>
            <tr>
              <th>Review</th>
              <th className="num">Model calls</th>
              <th className="num">Cost</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {summary?.recent_pr_costs.map((entry) => (
              <tr key={entry.review_id}>
                <td className="mono">{entry.review_id.slice(0, 8)}</td>
                <td className="num">{entry.llm_calls}</td>
                <td className="num mono">{money(entry.cost_usd)}</td>
                <td>
                  <a href={`/reviews/${entry.review_id}`}>open</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {summary && summary.recent_pr_costs.length === 0 ? (
          <p className="muted">Nothing in the window.</p>
        ) : null}
      </Panel>
    </>
  );
}
