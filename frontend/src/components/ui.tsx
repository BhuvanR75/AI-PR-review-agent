import type { ReactNode } from "react";

import { SEVERITY_COLOUR, SEVERITY_ORDER, STATUS_COLOUR, isSeverity } from "@/lib/format";
import type { ReviewStatus, Severity } from "@/lib/types";

export function Pill({ label, colour }: { label: string; colour: string }): ReactNode {
  return (
    <span className="pill" style={{ background: colour }}>
      {label}
    </span>
  );
}

export function SeverityPill({ severity }: { severity: string }): ReactNode {
  const colour = isSeverity(severity) ? SEVERITY_COLOUR[severity] : "#667085";
  return <Pill label={severity} colour={colour} />;
}

export function StatusPill({ status }: { status: ReviewStatus }): ReactNode {
  return <Pill label={status.replace(/_/g, " ")} colour={STATUS_COLOUR[status] ?? "#667085"} />;
}

/** Severity counts, worst first, with zeroes omitted -- an empty category is not a
 * clean bill of health, and printing "0 CRITICAL" invites reading it as one. */
export function SeverityCounts({ counts }: { counts: Record<string, number> }): ReactNode {
  const present = SEVERITY_ORDER.filter((s: Severity) => (counts[s] ?? 0) > 0);
  if (present.length === 0) {
    return <span className="muted">no findings</span>;
  }
  return (
    <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
      {present.map((severity) => (
        <span key={severity} className="tag" style={{ color: SEVERITY_COLOUR[severity] }}>
          {counts[severity]} {severity}
        </span>
      ))}
    </span>
  );
}

/** Confidence is the pass's own estimate, not a calibrated probability, and the UI
 * says so rather than dressing it up as one. */
export function Confidence({ value }: { value: number | null }): ReactNode {
  if (value === null) return <span className="muted">unknown</span>;
  const colour = value >= 0.75 ? "#027a48" : value >= 0.5 ? "#b54708" : "#b42318";
  return (
    <span title="The pass's own estimate of how sure it is; not a calibrated probability.">
      <span className="mono" style={{ color: colour }}>
        {value.toFixed(2)}
      </span>
    </span>
  );
}

export function Banner({
  kind,
  children,
}: {
  kind: "error" | "ok" | "warn";
  children: ReactNode;
}): ReactNode {
  return <div className={`banner ${kind}`}>{children}</div>;
}

export function Panel({
  title,
  subtitle,
  children,
}: {
  title?: string;
  subtitle?: string;
  children: ReactNode;
}): ReactNode {
  return (
    <section className="panel">
      {title ? <h2>{title}</h2> : null}
      {subtitle ? <div className="sub">{subtitle}</div> : null}
      {children}
    </section>
  );
}
