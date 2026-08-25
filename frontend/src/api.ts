export type IncidentStatus =
  | "REPORTED" | "TRIAGED" | "CONTAINED" | "DISPATCHING" | "SCHEDULED"
  | "IN_PROGRESS" | "VERIFYING" | "PROVISIONALLY_RESOLVED" | "CLOSED"
  | "ESCALATED" | "CANCELLED" | "REOPENED";

export type TimelineEntry = {
  event_id: string;
  at: string;
  kind: string;
  rule_id?: string;
  state_from?: IncidentStatus;
  state_to?: IncidentStatus;
  metadata: Record<string, unknown>;
};

export type Incident = {
  incident_id: string;
  property_id: string;
  tenant_id: string;
  status: IncidentStatus;
  report_text: string;
  voice_transcript?: string;
  media_ids: string[];
  facts?: {
    issue_type: string;
    severity: string;
    water_visible: boolean;
    electrical_hazard: boolean;
    estimated_cost?: number;
    affected_rooms: string[];
    source_confidence: number;
  };
  containment_instructions?: string;
  assigned_vendor_id?: string;
  eta?: string;
  work_order?: { estimated_cost: number; spending_limit: number; currency: string; status: string };
  approval?: { status: string; requested_amount: number; limit: number };
  last_evidence?: { passed: boolean; blocking_reasons: string[]; photo_confidence: number; invoice_total?: number };
  warranty_expires_at?: string;
  timeline: TimelineEntry[];
};

export type RuntimeMetadata = {
  environment: string;
  deployment: string;
  facts_provider: string;
  facts_model: string;
  storage_backend: string;
  eventing: string;
  messaging_provider: string;
  demo_clock_enabled: boolean;
  demo_timings_seconds: Record<string, number>;
  synthetic_data_only: boolean;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  if (!response.ok) throw new Error((await response.text()) || `Request failed: ${response.status}`);
  return response.json() as Promise<T>;
}

export const api = {
  runtime: () => request<RuntimeMetadata>("/api/runtime"),
  seed: () => request<Incident>("/api/demo/seed", { method: "POST" }),
  report: (report: Record<string, unknown>) => request<Incident>("/api/incidents", { method: "POST", body: JSON.stringify(report) }),
  incident: (id: string) => request<Incident>(`/api/incidents/${id}`),
  action: (id: string, action: string, payload: Record<string, unknown> = {}) =>
    request<Incident>(`/api/incidents/${id}/actions`, {
      method: "POST",
      body: JSON.stringify({ action, payload, event_id: `${action}:${crypto.randomUUID()}` }),
    }),
};
