import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Check,
  CircleAlert,
  Cloud,
  Clock3,
  Droplets,
  FileCheck2,
  LockKeyhole,
  MessageCircle,
  Play,
  Radio,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  UserRound,
  Wrench,
} from "lucide-react";
import { api, CommunicationRecord, Incident, MediaDescriptor, RuntimeMetadata } from "./api";

function statusLabel(status: string) {
  return status.replaceAll("_", " ").toLowerCase();
}

function timelineLabel(kind: string) {
  return kind.replaceAll("_", " ");
}

function shortId(value: string) {
  return value.length > 24 ? `${value.slice(0, 10)}…${value.slice(-8)}` : value;
}

function formatTime(value: string) {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function displayParty(role: CommunicationRecord["sender_role"], id: string) {
  if (role === "agent") return "No More Buckets agent";
  if (role === "scheduler") return "Workflow scheduler";
  if (role === "tenant") return "Tenant";
  if (role === "vendor") return id === "vendor-a" ? "Vendor A" : id === "vendor-b" ? "Vendor B" : `Vendor ${id}`;
  return id;
}

function MediaView({ media }: { media: MediaDescriptor }) {
  if (media.mime_type.startsWith("image/")) {
    return <img className="media-preview" src={media.url} alt={media.filename} />;
  }
  if (media.mime_type.startsWith("audio/")) {
    return <audio className="audio-player" controls preload="metadata"><source src={media.url} type={media.mime_type} />Your browser cannot play this voice note.</audio>;
  }
  return <span className="media-file"><FileCheck2 size={14} /> {media.filename}</span>;
}

function CommunicationCard({ communication, mediaById }: { communication: CommunicationRecord; mediaById: Map<string, MediaDescriptor> }) {
  return (
    <article className="communication-card">
      <div className="communication-heading">
        <span className="party"><UserRound size={13} /> {displayParty(communication.sender_role, communication.sender_id)}</span>
        <ArrowRight size={13} className="muted-icon" />
        <span className="party">{displayParty(communication.recipient_role, communication.recipient_id)}</span>
      </div>
      <div className="communication-meta">
        <span>{communication.channel}</span><span>{communication.message_type}</span><span>{formatTime(communication.timestamp)}</span>
        <span className={`delivery delivery-${communication.delivery_status}`}>{communication.delivery_status}</span>
      </div>
      {communication.text && <p>{communication.text}</p>}
      {communication.provider_message_id && <code className="provider-id">provider · {shortId(communication.provider_message_id)}</code>}
      {communication.media_ids.length > 0 && <div className="communication-media">{communication.media_ids.map((mediaId) => {
        const media = mediaById.get(mediaId);
        return media ? <MediaView key={mediaId} media={media} /> : <span className="media-file" key={mediaId}>media unavailable</span>;
      })}</div>}
    </article>
  );
}

function App() {
  const [incident, setIncident] = useState<Incident | null>(null);
  const [runtime, setRuntime] = useState<RuntimeMetadata | null>(null);
  const [communications, setCommunications] = useState<CommunicationRecord[]>([]);
  const [media, setMedia] = useState<MediaDescriptor[]>([]);
  const [replayRunning, setReplayRunning] = useState(false);
  const [error, setError] = useState("");
  const [timeoutCountdown, setTimeoutCountdown] = useState<number | null>(null);
  const selectedIncidentId = useRef<string | null>(null);

  useEffect(() => {
    api.runtime().then(setRuntime).catch((err: Error) => setError(err.message));
  }, []);

  useEffect(() => {
    let active = true;
    async function refreshLiveIncident() {
      try {
        const incidents = await api.incidents();
        if (!active) return;
        const next = [...incidents].sort((left, right) => right.updated_at.localeCompare(left.updated_at));
        const selected = next.find((candidate) => candidate.incident_id === selectedIncidentId.current) ?? next[0];
        if (!selected) {
          selectedIncidentId.current = null;
          setIncident(null); setCommunications([]); setMedia([]);
          return;
        }
        selectedIncidentId.current = selected.incident_id;
        const [current, nextCommunications, nextMedia] = await Promise.all([
          api.incident(selected.incident_id), api.communications(selected.incident_id), api.media(selected.incident_id),
        ]);
        if (!active) return;
        setIncident(current); setCommunications(nextCommunications); setMedia(nextMedia);
      } catch (err) {
        if (active && !replayRunning) setError(err instanceof Error ? err.message : "Live update failed");
      }
    }
    void refreshLiveIncident();
    const timer = window.setInterval(() => void refreshLiveIncident(), 1000);
    return () => { active = false; window.clearInterval(timer); };
  }, [replayRunning]);

  const mediaById = useMemo(() => new Map(media.map((item) => [item.media_id, item])), [media]);
  const tenantCommunications = communications.filter((item) => item.sender_role === "tenant" || item.recipient_role === "tenant");
  const vendorCommunications = communications.filter((item) => item.sender_role === "vendor" || item.recipient_role === "vendor");
  const agentCommunications = communications.filter((item) => item.sender_role === "scheduler" || item.sender_role === "system");
  const reportAudio = tenantCommunications.flatMap((item) => item.media_ids.map((id) => mediaById.get(id))).find((item) => item?.mime_type.startsWith("audio/"));
  const pendingAttempt = incident?.vendor_attempts.find((attempt) => attempt.outcome === "pending");
  const timeoutSchedule = incident?.timeline.find((entry) => entry.kind === "vendor_timeout_scheduled");
  const fallback = incident?.timeline.find((entry) => entry.kind === "vendor_dispatch_outcome" && (entry.metadata.outcome === "decline" || entry.metadata.outcome === "timeout"));
  const accepted = incident?.timeline.find((entry) => entry.kind === "vendor_dispatch_outcome" && entry.metadata.outcome === "accept");

  useEffect(() => {
    if (!pendingAttempt || !timeoutSchedule) {
      setTimeoutCountdown(null);
      return;
    }
    const configuredSeconds = Number(timeoutSchedule.metadata.timeout_seconds ?? 0);
    const deadline = timeoutSchedule.metadata.deadline_at ? new Date(String(timeoutSchedule.metadata.deadline_at)).getTime() : 0;
    const calculate = () => {
      if (runtime?.demo_clock_enabled) return Math.max(0, configuredSeconds - Math.floor((Date.now() - performance.timeOrigin) / 1000));
      return deadline ? Math.max(0, Math.ceil((deadline - Date.now()) / 1000)) : configuredSeconds;
    };
    setTimeoutCountdown(calculate());
    const timer = window.setInterval(() => setTimeoutCountdown(calculate()), 1000);
    return () => window.clearInterval(timer);
  }, [incident?.incident_id, pendingAttempt?.event_id, timeoutSchedule?.event_id, runtime?.demo_clock_enabled]);

  async function startReplay() {
    setError(""); setReplayRunning(true);
    try {
      let current = await api.seed();
      selectedIncidentId.current = current.incident_id; setIncident(current);
      await new Promise((resolve) => setTimeout(resolve, 450));
      if (current.status === "DISPATCHING") {
        current = await api.action(current.incident_id, "vendor_timeout", { vendor_id: "vendor-a" }); setIncident(current);
      }
      await new Promise((resolve) => setTimeout(resolve, 450));
      current = await api.action(current.incident_id, "eta"); setIncident(current);
      await new Promise((resolve) => setTimeout(resolve, 450));
      current = await api.action(current.incident_id, "work_started"); setIncident(current);
      await new Promise((resolve) => setTimeout(resolve, 450));
      const photoContentBase64 = "iVBORw0KGgoAAAANSUhEUgAAAKAAAABkCAIAAACO1KzYAAABPUlEQVR42u3csQ3CMBBAUS/EACxAwRZULMAQdIzARIgWpUpJywYU6ZAICDvYuTzp95F4TXJ3InWPuwKX/ASABViABViABViAAQuwAAuwAAuwAAMWYAEWYAEWYAEWYMACLMACLMACLMCABViABVgtAl/7m+oGGDBgwIABC7AAqy3gzLb7gyIPOugCBgwYMGDAgAEDBgwYMOAlAWe2PncjrU6XUo0/aMhFx+TABUV/8AY8FfDfXD9KAy5ZRdd30oAD0r4wAw6rOwQ4sm77xolubONEN7YxYMCAAQMGDBiwt2jfwXRNskwrzaKXtHKwTSqwNwQcR3p2a/8Z/0dH9YsOwDO+4PnmJgtw5TO8/Cu+AHd36bjbKHCAAQuwAAuwAAuwAAMWYAEWYAEWYAEGLMACLMACLMACLMCABViAVa8n+J+v7cZifl4AAAAASUVORK5CYII=";
      const photoBytes = Uint8Array.from(atob(photoContentBase64), (character) => character.charCodeAt(0));
      const photo = { asset_id: "media-completion-photo", filename: "after-repair.png", mime_type: "image/png", size_bytes: photoBytes.length, sha256: "", content_base64: photoContentBase64, source: "vendor" };
      current = await api.action(current.incident_id, "completion", {
        photo: { ...photo, sha256: await digestBytes(photoBytes) },
        invoice: { invoice_id: "invoice-demo-001", vendor_id: current.assigned_vendor_id ?? "vendor-b", currency: "SGD", total: 220, line_items: [{ description: "leak repair labor and parts", quantity: 1, unit_price: 220 }] },
      }); setIncident(current);
      await new Promise((resolve) => setTimeout(resolve, 450));
      current = await api.action(current.incident_id, "tenant_confirm"); setIncident(current);
    } catch (err) { setError(err instanceof Error ? err.message : "Replay failed"); }
    finally { setReplayRunning(false); }
  }

  async function digestBytes(bytes: Uint8Array) {
    const hash = await crypto.subtle.digest("SHA-256", bytes.buffer as ArrayBuffer);
    return Array.from(new Uint8Array(hash)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function reset() {
    setError(""); selectedIncidentId.current = null; setIncident(null); setCommunications([]); setMedia([]);
    await fetch("/api/demo/reset", { method: "POST" });
  }

  async function runException() {
    setError("");
    try {
      const current = await api.report({ property_id: "demo-tampines-101", tenant_id: "tenant-demo-exception", report_text: "Water is spraying beside a wet electrical outlet and I see sparks.", voice_transcript: "This feels dangerous and I cannot safely reach the shutoff.", idempotency_key: `exception:${crypto.randomUUID()}`, source_channel: "local_demo" });
      selectedIncidentId.current = current.incident_id; setIncident(current);
    } catch (err) { setError(err instanceof Error ? err.message : "Exception path failed"); }
  }

  return (
    <main className="app-shell">
      <nav className="topbar"><div className="brand"><div className="brand-mark"><Droplets size={18} /></div><span>no more buckets</span></div><div className="topbar-right"><span className="live-dot" /> LIVE INCIDENT CONTROL ROOM <span className="pill">{runtime?.demo_clock_enabled ? "DEMO CLOCK ENABLED" : "LIVE CLOCK"}</span><span className="pill">TELEGRAM PRIMARY</span></div></nav>
      <section className="hero"><div className="eyebrow"><Radio size={14} /> REAL REPORTS · REAL BACKEND EVENTS</div><h1>From first drip<br /><em>to done.</em></h1><p className="lede">A live control room for bounded leak response. Send a report to Telegram and watch the persisted incident, conversations, and decisions arrive here.</p><div className="hero-actions"><button className="primary-button" onClick={startReplay} disabled={replayRunning}><Play size={16} fill="currentColor" /> {replayRunning ? "Replaying deterministic scenario…" : "Replay deterministic scenario"}</button><button className="ghost-button" onClick={runException} disabled={replayRunning}><CircleAlert size={15} /> Try safety exception</button>{(incident || replayRunning) && <button className="ghost-button" onClick={reset}><RotateCcw size={15} /> Reset</button>}</div>{error && <div className="error-banner"><CircleAlert size={16} /> {error}</div>}</section>
      <section className="proof-strip"><div><span className="proof-icon"><Cloud size={16} /></span><span><small>EXECUTION</small><strong>{runtime?.deployment ?? "loading"}</strong></span></div><div><span className="proof-icon"><Sparkles size={16} /></span><span><small>OBSERVATION LAYER</small><strong>{runtime?.facts_provider ?? "loading"} · {runtime?.facts_model ?? ""}</strong></span></div><div><span className="proof-icon"><LockKeyhole size={16} /></span><span><small>SOURCE OF TRUTH</small><strong>{runtime?.storage_backend ?? "loading"} · append-only timeline</strong><small className="timing-note">{runtime?.demo_clock_enabled ? "Demo clock enabled · 8s / 12s / 15s / 30s" : "Live SLA timers"}</small></span></div></section>
      <section className="live-banner"><span className="live-dot" /><strong>LIVE BACKEND FEED</strong><span>Short polling every second · no page refresh</span><span className="live-banner-right">{incident ? `Watching ${shortId(incident.incident_id)}` : "Waiting for tenant report"}<RefreshCw size={13} /></span></section>

      {!incident ? <section className="waiting-room panel"><Activity size={34} /><span className="kicker">PRIMARY EXPERIENCE</span><h2>Waiting for a real tenant report</h2><p>Tenant text, photo, or voice notes from Telegram will create the incident. The local adapter is available for development; the deterministic replay above is secondary.</p><div className="waiting-hint"><MessageCircle size={16} /> Every contact will appear with channel, sender, recipient, timestamp, and delivery status.</div></section> : <>
        <section className="incident-header panel"><div><span className="kicker">ACTIVE INCIDENT · {incident.incident_id}</span><h2>{incident.report_text || "Tenant submitted a multimodal report"}</h2><span className="incident-subline">{incident.property_id} · {incident.tenant_id} · updated {formatTime(incident.updated_at)}</span></div><span className="state-chip">{incident.status}</span></section>
        <section className="lane-grid">
          <div className="lane panel"><div className="lane-heading"><div><span className="kicker">LANE 01</span><h2><MessageCircle size={18} /> Tenant conversation</h2></div><span className="event-count">{tenantCommunications.length} contacts</span></div>{tenantCommunications.length ? <div className="communication-list">{tenantCommunications.map((item) => <CommunicationCard key={item.communication_id} communication={item} mediaById={mediaById} />)}</div> : <div className="empty-state compact"><MessageCircle size={24} /><p>Waiting for the tenant’s Telegram report.</p></div>}</div>
          <div className="lane panel"><div className="lane-heading"><div><span className="kicker">LANE 02</span><h2><ShieldCheck size={18} /> Agent decisions / state</h2></div><span className="state-chip">{incident.status}</span></div><div className="decision-summary"><div className="decision-state"><span>WORKFLOW STATE</span><strong>{incident.status}</strong></div><div className="decision-stats"><span><Clock3 size={13} /> {incident.vendor_attempts.length} vendor attempts</span><span><LockKeyhole size={13} /> S$ {incident.work_order?.spending_limit.toFixed(0) ?? "250"} autonomous cap</span></div></div>{incident.report_assessment && <div className="assessment-box"><div className="assessment-title"><Sparkles size={14} /> MULTIMODAL ASSESSMENT · {Math.round(incident.report_assessment.confidence * 100)}% confidence</div><p className="transcript-label">Faithful voice transcript</p><p className="transcript">{incident.report_assessment.voice_transcript || "No voice transcript supplied."}</p>{reportAudio && <MediaView media={reportAudio} />}<div className="assessment-facts"><span>{incident.report_assessment.facts.issue_type}</span><span>{incident.report_assessment.facts.severity}</span><span>{incident.report_assessment.facts.water_visible ? "water visible" : "no water visible"}</span></div>{incident.report_assessment.missing_information.length > 0 && <small className="warning-note">Missing: {incident.report_assessment.missing_information.join(", ")}</small>}</div>}{incident.containment_instructions && <div className="containment"><div className="containment-title"><ShieldCheck size={15} /> PROPERTY-SPECIFIC CONTAINMENT SENT</div><p>{incident.containment_instructions}</p></div>}{agentCommunications.length > 0 && <div className="agent-contacts">{agentCommunications.map((item) => <CommunicationCard key={item.communication_id} communication={item} mediaById={mediaById} />)}</div>}<div className="policy-note"><LockKeyhole size={14} /><span>Gemini observes. Deterministic rules authorize safety, spend, access, dispatch, and closure.</span></div></div>
          <div className="lane panel"><div className="lane-heading"><div><span className="kicker">LANE 03</span><h2><Wrench size={18} /> Vendor conversation</h2></div><span className="event-count">{vendorCommunications.length} contacts</span></div>{vendorCommunications.length ? <div className="communication-list">{vendorCommunications.map((item) => <CommunicationCard key={item.communication_id} communication={item} mediaById={mediaById} />)}</div> : <div className="empty-state compact"><Wrench size={24} /><p>Dispatch contacts will appear after triage.</p></div>}<div className="vendor-proof"><div className={pendingAttempt ? "proof-active" : fallback ? "proof-active" : ""}>{pendingAttempt ? <Clock3 size={14} /> : <AlertTriangle size={14} />} Vendor A {pendingAttempt ? `timeout in ${timeoutCountdown ?? "…"}s` : fallback ? "timed out / failed" : "awaiting response"}</div><div className={accepted ? "proof-active" : ""}><Check size={14} /> Vendor B {accepted ? "fallback accepted" : "standby"}</div></div></div>
        </section>
        <section className="lower-grid"><div className="timeline-card panel"><div className="panel-heading"><div><span className="kicker">PERSISTED AUDIT TIMELINE</span><h2>State changes and rule outcomes</h2></div><span className="event-count">{incident.timeline.length} events</span></div><div className="timeline">{incident.timeline.map((entry) => <div className="timeline-item" key={`${entry.event_id}-${entry.kind}`}><div className="timeline-dot" /><div className="timeline-copy"><div className="timeline-meta"><span>{formatTime(entry.at)}</span>{entry.rule_id && <code>{entry.rule_id}</code>}</div><strong>{timelineLabel(entry.kind)}</strong>{entry.state_to && <span className="transition">{entry.state_from} → {entry.state_to}</span>}{Boolean(entry.metadata.vendor_id) && <span className="timeline-detail">{String(entry.metadata.vendor_id)} · {String(entry.metadata.outcome ?? "")}</span>}{Boolean(entry.metadata.blocking_reasons) && <span className="timeline-detail danger-text">{String(entry.metadata.blocking_reasons)}</span>}</div></div>)}</div></div><div className="outcome-card panel"><div className="panel-heading"><div><span className="kicker">CONTROL GATES</span><h2>Exceptions stay narrow.</h2></div><Activity size={21} /></div><div className="guardrail-list"><div className={`guardrail ${fallback ? "active" : ""}`}><span className="guardrail-icon"><CircleAlert size={15} /></span><span><strong>Vendor fallback</strong><small>{fallback ? "Next eligible vendor contacted" : "Bounded vendor ranking"}</small></span></div><div className={`guardrail ${incident.approval ? "active" : ""}`}><span className="guardrail-icon"><LockKeyhole size={15} /></span><span><strong>Spending authority</strong><small>{incident.approval ? "Approval required" : "S$250 autonomous limit"}</small></span></div><div className={`guardrail ${incident.last_evidence?.passed ? "active" : ""}`}><span className="guardrail-icon"><ShieldCheck size={15} /></span><span><strong>Evidence gate</strong><small>{incident.last_evidence?.passed ? "Photo + invoice verified" : "Missing or mismatched evidence blocks close"}</small></span></div><div className={`guardrail ${incident.status === "CLOSED" ? "active" : ""}`}><span className="guardrail-icon"><Check size={15} /></span><span><strong>Current state</strong><small>{statusLabel(incident.status)}</small></span></div></div><div className="final-state"><span>WORKFLOW STATE</span><strong>{incident.status}</strong>{incident.warranty_expires_at && <small>Warranty through {new Date(incident.warranty_expires_at).toLocaleDateString()}</small>}</div></div></section>
      </>}
      <footer><span>NO MORE BUCKETS · ATA HACKATHON</span><span>SAFE AUTOMATION FOR SMALL RENTALS · TELEGRAM + BACKEND EVENTS</span></footer>
    </main>
  );
}

export default App;
