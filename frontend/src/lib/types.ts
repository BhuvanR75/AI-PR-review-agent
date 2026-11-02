/**
 * The API contract, mirrored from `backend/models/review.py`.
 *
 * Kept as plain interfaces rather than generated from the OpenAPI schema because the
 * console is small and a generated client would hide which fields the UI actually
 * depends on. If the backend contract changes, `npm run typecheck` fails loudly here.
 */

export type Severity = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "INFO";

export type ReviewStatus =
  | "queued"
  | "running"
  | "pending_approval"
  | "approved"
  | "posted"
  | "rejected"
  | "failed"
  | "superseded";

export type HitlState = "awaiting" | "approved" | "rejected" | "superseded";

export type Decision = "post" | "human_approval" | "escalate";

export interface Finding {
  agent_type: string;
  severity: Severity;
  category: string;
  summary: string;
  file_path: string;
  line_start: number | null;
  line_end: number | null;
  suggestion: string | null;
  confidence: number;
  rationale: string;
  agreed_by: string[];
  incomplete: boolean;
  dedup_key?: string;
}

export interface ReviewCost {
  usd: number;
  tokens_in: number;
  tokens_out: number;
  llm_calls: number;
  by_agent: Record<string, number>;
}

export interface ReviewSummary {
  id: string;
  repo: string;
  pr_number: number;
  title: string | null;
  author: string | null;
  head_sha: string;
  status: ReviewStatus;
  decision: Decision | null;
  hitl_state: HitlState | null;
  overall_confidence: number | null;
  finding_count: number;
  severity_counts: Record<string, number>;
  cost: ReviewCost;
  created_at: string;
  updated_at: string;
}

export interface ReviewDetail extends ReviewSummary {
  findings: Finding[];
  /** Row identities, positionally aligned with `findings`. Used to post a subset. */
  finding_ids: string[];
  errors: string[];
  github_review_url: string | null;
  hitl_notes: string | null;
  /** The reviewed diff, persisted at review time. Null for pre-artifact reviews. */
  diff: string | null;
}

// --- GitHub connection & browsing -------------------------------------------

export interface ConnectionStatus {
  connected: boolean;
  login: string | null;
  scopes: string[];
  fingerprint: string | null;
  mode: string;
  message: string;
}

export interface ConnectResponse {
  connected: boolean;
  login: string;
  name: string | null;
  scopes: string[];
  fingerprint: string;
  dry_run: boolean;
  message: string;
}

export interface RepoSummary {
  full_name: string;
  description: string | null;
  private: boolean;
  pushed_at: string | null;
  allowed: boolean;
}

export interface PullRequestSummary {
  repo: string;
  number: number;
  title: string;
  author: string;
  head_sha: string;
  draft: boolean;
  updated_at: string | null;
}

export interface TriggerResponse {
  review_id: string;
  status: string;
  created: boolean;
  message: string;
}

export interface AgentEvent {
  ts: string;
  review_id: string | null;
  agent: string;
  span_id: string;
  parent_span: string | null;
  event_type: string;
  model: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: number | null;
  latency_ms: number | null;
  outcome: string | null;
  confidence: number | null;
  payload: Record<string, unknown> | null;
}

export interface TraceResponse {
  review_id: string;
  events: AgentEvent[];
  cost: ReviewCost;
  total_events: number;
}

export interface Explanation {
  locator: string;
  agent: string;
  severity: Severity;
  model: string | null;
  prompt_version: string | null;
  prompt_hash: string | null;
  cost_usd: number;
  latency_ms: number | null;
  agreeing_agents: string[];
  evidence: string | null;
  reproducible: boolean;
  caveat: string | null;
}

export interface QueueDepth {
  queued: number;
  running: number;
  pending_approval: number;
  oldest_pending_seconds: number | null;
}

export interface HitlActionResponse {
  review_id: string;
  status: ReviewStatus;
  hitl_state: HitlState;
  posted_finding_ids: string[];
  github_review_url: string | null;
  message: string;
}

export interface AgentHealth {
  agent: string;
  llm_calls: number;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  avg_latency_ms: number;
  max_latency_ms: number;
  p95_latency_ms: number | null;
  rejected: number;
  escalated: number;
  decisions: number;
  rejection_rate: number;
  escalation_rate: number;
  source: "raw" | "rollup";
}

export interface EconomicsSummary {
  daily_cost_usd: number;
  budget_usd: number;
  remaining_usd: number;
  utilisation: number;
  by_agent: AgentHealth[];
  recent_pr_costs: { review_id: string; cost_usd: number; llm_calls: number }[];
  rollups_available: boolean;
}

export interface FeedbackSignal {
  agent: string;
  approvals: number;
  rejections: number;
  edits: number;
  rejection_rate: number;
  enough_evidence: boolean;
  recommendation: string;
}

export interface Specialist {
  agent: string;
  concern: string | null;
  capabilities: string;
  may_post_to_github: boolean;
  model: string;
  max_tokens: number;
  prompt: { version: string; hash: string };
}
