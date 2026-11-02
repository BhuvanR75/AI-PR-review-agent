/**
 * The API client.
 *
 * One place that knows the base URL, the bearer token and how an error surfaces. The
 * backend returns a `detail` string on 4xx/5xx, and that string is usually the useful
 * sentence ("a newer commit exists, so this review must not be posted"), so it is
 * lifted into the thrown error rather than replaced with "something went wrong".
 */

import type {
  ConnectionStatus,
  ConnectResponse,
  EconomicsSummary,
  Explanation,
  FeedbackSignal,
  HitlActionResponse,
  PullRequestSummary,
  QueueDepth,
  RepoSummary,
  ReviewDetail,
  ReviewSummary,
  Specialist,
  TraceResponse,
  TriggerResponse,
} from "./types";

export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080").replace(
  /\/$/,
  "",
);

const TOKEN = process.env.NEXT_PUBLIC_API_TOKEN ?? "dev-api-token";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  const text = await response.text();
  if (!response.ok) {
    let detail = text;
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      // Not JSON: keep the raw body, truncated so a stray HTML page is legible.
    }
    throw new ApiError(response.status, detail.slice(0, 400) || `HTTP ${response.status}`);
  }
  return (text ? JSON.parse(text) : null) as T;
}

export const api = {
  // --- github connection & browsing ------------------------------------------
  connectionStatus: (): Promise<ConnectionStatus> => request<ConnectionStatus>("/api/github/status"),

  connect: (token: string): Promise<ConnectResponse> =>
    request<ConnectResponse>("/api/github/connect", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),

  disconnect: (): Promise<{ connected: boolean; message: string }> =>
    request<{ connected: boolean; message: string }>("/api/github/disconnect", {
      method: "POST",
    }),

  listRepos: (): Promise<{ repos: RepoSummary[]; source: string }> =>
    request<{ repos: RepoSummary[]; source: string }>("/api/github/repos"),

  listPulls: (repo: string): Promise<{ repo: string; pulls: PullRequestSummary[]; source: string }> =>
    request<{ repo: string; pulls: PullRequestSummary[]; source: string }>(
      `/api/github/repos/${repo}/pulls`,
    ),

  triggerReview: (repo: string, prNumber: number): Promise<TriggerResponse> =>
    request<TriggerResponse>("/api/reviews/trigger", {
      method: "POST",
      body: JSON.stringify({ repo, pr_number: prNumber }),
    }),

  // --- reviews ---------------------------------------------------------------
  listReviews: (params: { status?: string; limit?: number } = {}): Promise<ReviewSummary[]> => {
    const query = new URLSearchParams();
    if (params.status) query.set("status", params.status);
    query.set("limit", String(params.limit ?? 50));
    return request<ReviewSummary[]>(`/api/reviews?${query.toString()}`);
  },

  getReview: (id: string): Promise<ReviewDetail> => request<ReviewDetail>(`/api/reviews/${id}`),

  getTrace: (id: string): Promise<TraceResponse> => request<TraceResponse>(`/api/reviews/${id}/trace`),

  getExplanation: (id: string): Promise<{ review_id: string; explanations: Explanation[] }> =>
    request<{ review_id: string; explanations: Explanation[] }>(`/api/reviews/${id}/explain`),

  // --- human decisions -------------------------------------------------------
  approve: (
    id: string,
    body: { actor: string; note?: string | null; finding_ids?: string[] | null },
  ): Promise<HitlActionResponse> =>
    request<HitlActionResponse>(`/api/hitl/${id}/approve`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  reject: (id: string, body: { actor: string; reason: string }): Promise<HitlActionResponse> =>
    request<HitlActionResponse>(`/api/hitl/${id}/reject`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  rerun: (
    id: string,
    body: { actor: string; note?: string | null; agents?: string[] | null },
  ): Promise<HitlActionResponse> =>
    request<HitlActionResponse>(`/api/hitl/${id}/rerun`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- operations ------------------------------------------------------------
  queueDepth: (): Promise<QueueDepth> => request<QueueDepth>("/api/hitl/queue"),

  economics: (hours = 24): Promise<EconomicsSummary> =>
    request<EconomicsSummary>(`/api/economics/summary?hours=${hours}`),

  feedback: (hours = 168): Promise<{ window_hours: number; signals: FeedbackSignal[]; note: string }> =>
    request<{ window_hours: number; signals: FeedbackSignal[]; note: string }>(
      `/api/economics/feedback?hours=${hours}`,
    ),

  alerts: (): Promise<{ count: number; alerts: { kind: string; severity: string; message: string }[] }> =>
    request<{ count: number; alerts: { kind: string; severity: string; message: string }[] }>(
      "/api/economics/alerts",
    ),

  agents: (): Promise<{ specialists: Specialist[]; orchestration: { engine: string; topology: string } }> =>
    request<{ specialists: Specialist[]; orchestration: { engine: string; topology: string } }>(
      "/api/meta/agents",
    ),

  capabilities: (): Promise<{ storage_lanes: Record<string, boolean>; note: string }> =>
    request<{ storage_lanes: Record<string, boolean>; note: string }>("/api/meta/capabilities"),
};
