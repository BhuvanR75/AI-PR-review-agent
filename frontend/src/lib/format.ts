import type { ReviewStatus, Severity } from "./types";

export const SEVERITY_ORDER: Severity[] = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"];

/** Severity colours. Critical is the only one that pages a human, so it is the only
 * one allowed to shout. */
export const SEVERITY_COLOUR: Record<Severity, string> = {
  CRITICAL: "#b42318",
  HIGH: "#b54708",
  MEDIUM: "#b25e09",
  LOW: "#475467",
  INFO: "#667085",
};

export const STATUS_COLOUR: Record<ReviewStatus, string> = {
  queued: "#475467",
  running: "#175cd3",
  pending_approval: "#b54708",
  approved: "#027a48",
  posted: "#027a48",
  rejected: "#b42318",
  failed: "#b42318",
  superseded: "#667085",
};

export function isSeverity(value: string): value is Severity {
  return (SEVERITY_ORDER as string[]).includes(value);
}

export function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function humanDuration(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds)) return "-";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export function money(usd: number): string {
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(6)}`;
  return `$${usd.toFixed(4)}`;
}

export function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

/** A finding's location, in the form GitHub would show it. */
export function locator(filePath: string, line: number | null): string {
  return line === null ? filePath : `${filePath}:${line}`;
}
