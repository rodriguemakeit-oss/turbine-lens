/**
 * Turbine Lens API client — typed wrappers + SSE streaming helper.
 */

const API_BASE = "/api";

// --- Types ----------------------------------------------------------------

export interface SessionSummary {
  id: string;
  title: string | null;
  owner_email: string;
  message_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface TurnMeta {
  cost_usd?: number | null;
  duration_ms?: number | null;
  num_turns?: number | null;
  genie_count?: number;
  sql_count?: number;
}

export interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
  steps_json?: string[];
  thinking?: string;
  meta?: TurnMeta;
  trace_id?: string;
  session_id?: string;
  created_at: string | null;
}

export interface InvokeAgentResponse {
  execution_id: string;
  session_id: string;
}

/** One generated report. Immutable: a shared link always renders what its reader saw. */
export interface ReportVersion {
  id: string;
  version: number;
  created_at: string | null;
  created_by?: string | null;
  message_count?: number;
}

export interface StreamEvent {
  type: string;
  text?: string;
  thinking?: string;
  tool_name?: string;
  label?: string;
  tool_use_id?: string;
  error?: string;
  meta?: TurnMeta;
  trace_id?: string;
  session_id?: string;
  events_sent?: number;
  /** On a `queued` event: how many turns are ahead of this one. */
  ahead?: number;
  timestamp?: number;
}

export type GenieMode = "agent" | "classic";

export interface AppConfig {
  model: string;
  workspace_host: string;
  genie_spaces: string[];
  /** Persistence is actually usable (bootstrap succeeded). */
  persistence_enabled: boolean;
  genie_mode: GenieMode;
  /** Connection settings are present. Differs from the above when degraded. */
  persistence_configured: boolean;
}

export interface ActiveExecution {
  execution_id: string | null;
  /** running | complete | cancelled | error | interrupted */
  status: string | null;
  source?: "memory" | "lakebase";
  event_count?: number;
  events: StreamEvent[];
}

export interface UserInfo {
  email: string;
  name: string | null;
}

// --- Fetch helpers --------------------------------------------------------

async function fetchJSON<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}


// --- Cost forecast --------------------------------------------------------
/** A value plus where it came from. Provenance travels with every figure so the page
 *  can never present an assumption as if it were a measured price. */
export interface Provenanced {
  value: number | null;
  source: "measured" | "list_price" | "published" | "config" | "assumption" | "unknown";
  as_of: string;
  note: string;
}

export interface CostLine {
  key: string;
  label: string;
  driver: "users" | "concurrent";
  /** After this line's negotiated discount. `usd_list` is the Databricks list price. */
  usd: number;
  usd_list: number;
  /** % off list applied to THIS line — 0 for FMAPI, which is not in either discount group. */
  discount_pct: number;
  formula: string;
  detail: Record<string, number | string>;
}

export interface CostBreakpoint {
  key: string;
  label: string;
  unit_name: string;
  at_concurrent_users: number;
  units_now: number;
  units_after: number;
  /** Discounted at the same rate as the line it belongs to. */
  usd_step: number;
  usd_step_list: number;
  discount_pct: number;
}

export interface CostForecast {
  inputs: { users: number; concurrent_users: number };
  assumptions: Record<string, number>;
  genie_on_behalf_of_user: boolean;
  hours: Record<string, number>;
  lines: CostLine[];
  total_usd_month: number;
  total_usd_month_list: number;
  /** The two negotiated rates, % off list: A = Apps + SQL warehouse, B = Genie + Lakebase. */
  discounts: { a: number; b: number };
  /** Derived from the mix, not negotiated: 1 − net/list. Falls as adoption grows, because
   *  the undiscounted LLM line grows fastest. */
  effective_discount_pct: number;
  usd_per_user_month: number | null;
  breakpoints: CostBreakpoint[];
  notes: string[];
  rates: Record<string, Provenanced>;
  rate_overrides: string[];
  missing_rates: string[];
  /** False while any price the arithmetic needs is unknown — show "pending", not a total. */
  ready: boolean;
}

/** One point on the cost curve. */
export interface CostCurvePoint {
  users: number;
  concurrent_users: number;
  total_usd_month: number;
  usd_per_user_month: number | null;
  lines: Record<string, number>;
}

export interface CostCurve {
  series: CostCurvePoint[];
  concurrency_ratio: number;
  scale_concurrency: boolean;
  ready: boolean;
}

export interface CostRates {
  rates: Record<string, Provenanced>;
  config: Record<string, Provenanced>;
  missing: string[];
  ready: boolean;
  assumption_defaults: Record<string, number>;
}

// --- API methods ----------------------------------------------------------

export const api = {
  getConfig: () => fetchJSON<AppConfig>("/config/config"),

  getUser: () => fetchJSON<UserInfo>("/config/user"),

  listSessions: () => fetchJSON<SessionSummary[]>("/sessions"),

  getSession: (id: string) =>
    fetchJSON<{
      session_id: string;
      title: string | null;
      owner_email: string | null;
      owner_name: string | null;
      /** Server-decided: true when the viewer is not the owner. Never inferred here. */
      read_only: boolean;
      messages: Message[];
    }>(`/sessions/${id}`),

  /** Is a turn already running for this session? Used to reattach after a reload. */
  getActiveExecution: (id: string, after = 0) =>
    fetchJSON<ActiveExecution>(`/sessions/${id}/active_execution?after=${after}`),

  createSession: (title?: string) =>
    fetchJSON<{ id: string; title: string }>("/sessions", {
      method: "POST",
      body: JSON.stringify({ title: title || "New session" }),
    }),

  deleteSession: (id: string) =>
    fetchJSON<{ success: boolean }>(`/sessions/${id}`, { method: "DELETE" }),

  /** Rename a session. Owner only — the server returns 403 otherwise. `title` in the
   *  response is what was actually stored (trimmed, single-lined, length-capped), so
   *  render that rather than what was submitted. */
  updateSessionTitle: (id: string, title: string) =>
    fetchJSON<{ success: boolean; title?: string }>(`/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),

  /** Prices, measured constants and assumption defaults, each with provenance. */
  getCostRates: () => fetchJSON<CostRates>("/cost/rates"),

  /** Monthly cost at a given adoption level. The arithmetic lives server-side so the
   *  page, a notebook and any report all produce the same numbers. */
  forecastCost: (body: {
    users: number;
    concurrent_users: number;
    assumptions?: Record<string, number | boolean>;
    rate_overrides?: Record<string, number>;
  }) =>
    fetchJSON<CostForecast>("/cost/forecast", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** The total across a range of user counts, for the chart. Computed server-side so the
   *  curve and the single-point forecast cannot drift apart. */
  costCurve: (body: {
    users: number;
    concurrent_users: number;
    users_max: number;
    points?: number;
    scale_concurrency?: boolean;
    assumptions?: Record<string, number | boolean>;
    rate_overrides?: Record<string, number>;
  }) =>
    fetchJSON<CostCurve>("/cost/curve", { method: "POST", body: JSON.stringify(body) }),

  /** Generate a new report version. Owner only — the server enforces it. */
  createReport: (sessionId: string) =>
    fetchJSON<ReportVersion & { flagged_figures: string[] }>(
      `/sessions/${sessionId}/report`,
      { method: "POST" }
    ),

  /** Existing versions, newest first. Old versions never change, so links keep working. */
  listReports: (sessionId: string) =>
    fetchJSON<ReportVersion[]>(`/sessions/${sessionId}/reports`),

invokeAgent: (message: string, sessionId?: string, genieMode?: string) =>
    fetchJSON<InvokeAgentResponse>("/invoke_agent", {
      method: "POST",
      body: JSON.stringify({ message, session_id: sessionId, genie_mode: genieMode }),
    }),

  stopStream: (executionId: string) =>
    fetchJSON<{ success: boolean }>(`/stop_stream/${executionId}`, {
      method: "POST",
    }),

  submitFeedback: (data: {
    session_id?: string;
    trace_id?: string;
    level: string;
    rating: string;
    reason?: string;
    comment?: string;
  }) =>
    fetchJSON<{ success: boolean; feedback_id: number | null }>("/feedback", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  getGenieMode: (sessionId?: string) =>
    fetchJSON<{ mode: GenieMode }>("/config/genie_mode" + (sessionId ? `?session_id=${sessionId}` : "")),

  setGenieMode: (mode: GenieMode, sessionId?: string) =>
    fetchJSON<{ mode: GenieMode }>("/config/genie_mode", {
      method: "POST",
      body: JSON.stringify({ mode, session_id: sessionId }),
    }),
};

// --- SSE streaming helper -------------------------------------------------

export async function streamProgress(
  executionId: string,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
  /** Resume from this cursor — used when reattaching to a turn already running. */
  startIndex = 0
): Promise<void> {
  let lastEventIndex = startIndex;

  while (true) {
    if (signal?.aborted) return;

    const res = await fetch(`${API_BASE}/stream_progress/${executionId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ last_event_index: lastEventIndex }),
      signal,
    });

    if (!res.ok) {
      throw new Error(`Stream error: HTTP ${res.status}`);
    }

    const reader = res.body?.getReader();
    if (!reader) throw new Error("No response body");

    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line.startsWith("data: ")) {
          try {
            const event: StreamEvent = JSON.parse(line.slice(6));
            onEvent(event);

            // Track event count for reconnection (skip terminal/control events)
            if (event.type === "window_expired") {
              lastEventIndex = event.events_sent || lastEventIndex;
            } else if (event.type === "done" || event.type === "complete" || event.type === "cancelled") {
              return;
            } else {
              lastEventIndex++;
            }
          } catch {
            // skip malformed SSE lines
          }
        }
      }
    }

    // Window expired — reconnect with lastEventIndex to avoid replaying old events
  }
}
