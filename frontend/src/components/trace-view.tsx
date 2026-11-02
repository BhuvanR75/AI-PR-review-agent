"use client";

import type { ReactNode } from "react";

import { Panel } from "@/components/ui";
import { money, relativeTime } from "@/lib/format";
import type { AgentEvent } from "@/lib/types";

const OUTCOME_MARK: Record<string, string> = {
  ok: "·",
  approved: "+",
  rejected: "×",
  escalated: "!",
  error: "!",
  incomplete: "…",
};

/**
 * The trace viewer.
 *
 * This is the same table the audit trail and the cost ledger read, in time order, which
 * is the whole point of the events spine: what you see here is what happened, not a
 * reconstruction assembled from several systems that could disagree.
 */
export function TraceView({ events }: { events: AgentEvent[] }): ReactNode {
  const calls = events.filter((e) => e.event_type === "llm.call");
  const totalCost = calls.reduce((sum, e) => sum + (e.cost_usd ?? 0), 0);

  return (
    <Panel
      title="Trace"
      subtitle={`${events.length} event(s) · ${calls.length} model call(s) · ${money(totalCost)} attributed`}
    >
      {events.length === 0 ? <p className="muted">Nothing recorded for this review.</p> : null}
      {events.map((event, index) => (
        <div className="trace-row" key={`${event.span_id}-${index}`}>
          <span className="muted mono">{relativeTime(event.ts)}</span>
          <span>
            <span className="tag">{event.agent}</span>
          </span>
          <span>
            <span className="mono">{event.event_type}</span>
            {event.outcome ? (
              <span className="muted">
                {" "}
                {OUTCOME_MARK[event.outcome] ?? ""} {event.outcome}
              </span>
            ) : null}
            {event.model ? <span className="muted"> · {event.model}</span> : null}
            {event.latency_ms !== null ? (
              <span className="muted"> · {event.latency_ms}ms</span>
            ) : null}
            {event.tokens_in !== null || event.tokens_out !== null ? (
              <span className="muted">
                {" "}
                · {event.tokens_in ?? 0}→{event.tokens_out ?? 0} tok
              </span>
            ) : null}
            {event.cost_usd ? <span className="muted"> · {money(event.cost_usd)}</span> : null}
            {event.payload && Object.keys(event.payload).length > 0 ? (
              <div className="mono muted" style={{ fontSize: 12, marginTop: 2 }}>
                {summarise(event.payload)}
              </div>
            ) : null}
          </span>
        </div>
      ))}
    </Panel>
  );
}

function summarise(payload: Record<string, unknown>): string {
  return Object.entries(payload)
    .map(([key, value]) => {
      if (Array.isArray(value)) return `${key}=[${value.length}]`;
      if (value === null || value === undefined) return `${key}=null`;
      if (typeof value === "object") return `${key}={…}`;
      const text = String(value);
      return `${key}=${text.length > 60 ? `${text.slice(0, 60)}…` : text}`;
    })
    .join("  ");
}
