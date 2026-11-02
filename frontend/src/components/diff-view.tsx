"use client";

import { useMemo } from "react";

import { SeverityPill } from "@/components/ui";
import type { Finding } from "@/lib/types";

/**
 * The diff view: the changed code, as it was reviewed.
 *
 * A unified diff is parsed into per-file sections of numbered lines, with added and
 * removed lines marked. Findings are anchored on the exact new-side line they name —
 * the property that makes a review legible: you see the code and the objection
 * side by side, at the line it refers to.
 *
 * The parser is deliberately tolerant: it is fed diffs from GitHub and from the demo
 * fixtures, and a malformed hunk degrades to showing the raw text rather than
 * throwing away the whole view.
 */

export interface DiffLine {
  kind: "context" | "added" | "removed" | "meta";
  /** New-side line number, where applicable. Findings anchor on this. */
  lineNo: number | null;
  text: string;
}

export interface FileDiff {
  path: string;
  lines: DiffLine[];
}

/** A finding attached to a file, plus whether its line actually exists in the diff. */
export interface Anchor {
  finding: Finding;
  /** Positionally aligned with the finding's row id, for the approve-subset flow. */
  findingIndex: number;
  lineNo: number | null;
  /** False when the finding names a line the diff does not contain. */
  resolved: boolean;
}

const HUNK_HEADER = /^@@\s*-\d+(?:,\d+)?\s*\+(\d+)(?:,\d+)?\s*@@/;
const FILE_HEADER = /^diff --git a\/(.+) b\/(.+)$/;

export function parseDiff(diff: string): FileDiff[] {
  const files: FileDiff[] = [];
  let current: FileDiff | null = null;
  let newLine: number | null = null;

  const push = (line: DiffLine) => {
    if (current) current.lines.push(line);
  };

  for (const raw of diff.split("\n")) {
    const fileMatch = FILE_HEADER.exec(raw);
    if (fileMatch) {
      current = { path: fileMatch[2], lines: [] };
      files.push(current);
      newLine = null;
      push({ kind: "meta", lineNo: null, text: raw });
      continue;
    }

    const hunkMatch = HUNK_HEADER.exec(raw);
    if (hunkMatch) {
      newLine = parseInt(hunkMatch[1], 10);
      push({ kind: "meta", lineNo: null, text: raw });
      continue;
    }

    if (!current) continue;

    if (raw.startsWith("+")) {
      push({ kind: "added", lineNo: newLine, text: raw.slice(1) });
      if (newLine !== null) newLine += 1;
    } else if (raw.startsWith("-")) {
      push({ kind: "removed", lineNo: null, text: raw.slice(1) });
    } else if (raw.startsWith("\\")) {
      // "\ No newline at end of file" — no line number, display only.
      push({ kind: "meta", lineNo: null, text: raw });
    } else {
      push({ kind: "context", lineNo: newLine, text: raw.replace(/^ /, "") });
      if (newLine !== null) newLine += 1;
    }
  }
  return files;
}

/** Anchor findings onto files. Returns also the ones that could not be placed. */
export function anchorFindings(files: FileDiff[], findings: Finding[]): {
  byFile: Map<string, Anchor[]>;
  unanchored: Anchor[];
} {
  const byFile = new Map<string, Anchor[]>();
  const unanchored: Anchor[] = [];

  findings.forEach((finding, findingIndex) => {
    const file = files.find((f) => f.path === finding.file_path);
    const lineNo = finding.line_start;
    const resolved =
      file !== undefined &&
      lineNo !== null &&
      file.lines.some((l) => l.lineNo === lineNo && l.kind !== "meta");
    const anchor: Anchor = { finding, findingIndex, lineNo, resolved };
    if (file !== undefined) {
      const list = byFile.get(file.path) ?? [];
      list.push(anchor);
      byFile.set(file.path, list);
    } else {
      unanchored.push(anchor);
    }
  });
  return { byFile, unanchored };
}

const LINE_BG: Record<DiffLine["kind"], string | undefined> = {
  added: "#e6ffec",
  removed: "#ffebe9",
  context: undefined,
  meta: "#f6f8fa",
};

export function DiffView({
  diff,
  findings,
}: {
  diff: string;
  findings: Finding[];
}): React.ReactNode {
  const files = useMemo(() => parseDiff(diff), [diff]);
  const { byFile, unanchored } = useMemo(() => anchorFindings(files, findings), [files, findings]);

  return (
    <>
      {files.map((file) => {
        const anchors = byFile.get(file.path) ?? [];
        const byLine = new Map<number, Anchor[]>();
        for (const a of anchors) {
          if (a.lineNo === null) continue;
          const list = byLine.get(a.lineNo) ?? [];
          list.push(a);
          byLine.set(a.lineNo, list);
        }

        return (
          <section key={file.path} style={{ marginBottom: 18 }}>
            <h3 className="mono" style={{ fontSize: 13, margin: "0 0 6px" }}>
              {file.path}
            </h3>
            <div
              className="mono"
              style={{
                border: "1px solid var(--border)",
                borderRadius: 8,
                overflowX: "auto",
                fontSize: 12.5,
                lineHeight: 1.45,
                background: "#fff",
              }}
            >
              {file.lines.map((line, i) => {
                const lineAnchors = line.lineNo !== null ? byLine.get(line.lineNo) : undefined;
                const isAnchorTarget =
                  line.kind === "added" && (lineAnchors?.length ?? 0) > 0;
                const bg =
                  isAnchorTarget ? "#fff3c2" /* finding anchor highlight */ : LINE_BG[line.kind];
                return (
                  <div
                    key={i}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "48px 1fr",
                      background: bg,
                      borderLeft: isAnchorTarget ? "3px solid #b54708" : "3px solid transparent",
                    }}
                  >
                    <span style={{ color: "var(--muted)", padding: "0 8px", textAlign: "right", userSelect: "none" }}>
                      {line.lineNo ?? ""}
                    </span>
                    <span style={{ padding: "0 10px", whiteSpace: "pre-wrap" }}>{line.text}</span>
                  </div>
                );
              })}
            </div>

            {/* Findings for this file, under the code they anchor to. */}
            {anchors.map((a) => (
              <article
                key={`${a.finding.file_path}:${a.finding.line_start}:${a.findingIndex}`}
                className="finding"
                style={{ borderLeftColor: "var(--border)", marginTop: 8 }}
              >
                <div className="row">
                  <strong>{a.finding.summary}</strong>
                  <span className="row" style={{ gap: 8 }}>
                    <SeverityPill severity={a.finding.severity} />
                    <span className="tag">{a.finding.agent_type}</span>
                    <span className="mono muted">
                      {a.resolved ? `line ${a.lineNo}` : a.finding.file_path}
                    </span>
                  </span>
                </div>
                <p style={{ margin: "6px 0" }}>{a.finding.rationale}</p>
                {a.finding.suggestion ? (
                  <p className="muted" style={{ margin: 0 }}>
                    <strong>Suggestion:</strong> {a.finding.suggestion}
                  </p>
                ) : null}
              </article>
            ))}
          </section>
        );
      })}

      {unanchored.length > 0 ? (
        <>
          <h3 style={{ fontSize: 14 }}>Findings without an anchor in this diff</h3>
          {unanchored.map((a) => (
            <article key={a.findingIndex} className="finding" style={{ borderLeftColor: "var(--border)" }}>
              <div className="row">
                <strong>{a.finding.summary}</strong>
                <span className="row" style={{ gap: 8 }}>
                  <SeverityPill severity={a.finding.severity} />
                  <span className="tag">{a.finding.agent_type}</span>
                  <span className="mono muted">{a.finding.file_path}</span>
                </span>
              </div>
              <p style={{ margin: "6px 0" }}>{a.finding.rationale}</p>
            </article>
          ))}
        </>
      ) : null}
    </>
  );
}
