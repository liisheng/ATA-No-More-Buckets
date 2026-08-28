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

export type ReportAssessment = {
  voice_transcript?: string;
  facts: {
    issue_type: string;
    severity: string;
    water_visible: boolean;
    water_source?: string;
    electrical_hazard: boolean;
    structural_hazard: boolean;
    gas_hazard: boolean;
    occupant_danger: boolean;
    uncontrolled_flooding: boolean;
    access_available: boolean;
    estimated_cost?: number;
    affected_rooms: string[];
    observed_text: string;
    evidence_refs: string[];
    source_confidence: number;
    uncertainties: string[];
  };
  conflicts: string[];
  missing_information: string[];
  confidence: number;
};

export type CommunicationRecord = {
  communication_id: string;
  incident_id: string;
  sender_role: "tenant" | "agent" | "vendor" | "scheduler" | "system";
  sender_id: string;
  recipient_role: "tenant" | "agent" | "vendor" | "scheduler" | "system";
  recipient_id: string;
  channel: string;
  direction: "inbound" | "outbound";
  message_type: "text" | "image" | "video" | "audio" | "button" | "invoice" | "system";
  text: string;
  media_ids: string[];
  provider_message_id?: string;
  delivery_status: "received" | "sent" | "delivered" | "failed" | "simulated" | "deduplicated";
  timestamp: string;
};

export type MediaDescriptor = {
  media_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  duration_seconds?: number;
  source: "tenant" | "vendor" | "system";
  url: string;
};

export type Incident = {
  incident_id: string;
  property_id: string;
  tenant_id: string;
  status: IncidentStatus;
  report_text: string;
  voice_transcript?: string;
  media_ids: string[];
  report_assessment?: ReportAssessment;
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
  vendor_attempts: { vendor_id: string; outcome: string; attempt_id: string; event_id: string; at: string; deadline_at?: string }[];
  eta?: string;
  work_order?: { estimated_cost?: number; authorized_amount: number; spending_limit: number; currency: string; status: string; scope?: string };
  approval?: { status: string; requested_amount: number; limit: number };
  last_evidence?: { passed: boolean; blocking_reasons: string[]; photo_confidence: number; invoice_total?: number };
  warranty_expires_at?: string;
  created_at: string;
  updated_at: string;
  timeline: TimelineEntry[];
};

export type TelegramDraft = {
  draft_id: string;
  tenant_id: string;
  property_id: string;
  text_parts: string[];
  media: MediaDescriptor[];
  communications: CommunicationRecord[];
  created_at: string;
  updated_at: string;
  expires_at: string;
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
  incidents: () => request<Incident[]>("/api/incidents"),
  drafts: () => request<TelegramDraft[]>("/api/drafts"),
  seed: () => request<Incident>("/api/demo/seed", { method: "POST" }),
  report: (report: Record<string, unknown>) => request<Incident>("/api/incidents", { method: "POST", body: JSON.stringify(report) }),
  incident: (id: string) => request<Incident>(`/api/incidents/${id}`),
  communications: (id: string) => request<CommunicationRecord[]>(`/api/incidents/${id}/communications`),
  media: (id: string) => request<MediaDescriptor[]>(`/api/incidents/${id}/media`),
  action: (id: string, action: string, payload: Record<string, unknown> = {}) =>
    request<Incident>(`/api/incidents/${id}/actions`, {
      method: "POST",
      body: JSON.stringify({ action, payload, event_id: `${action}:${crypto.randomUUID()}` }),
    }),
};
