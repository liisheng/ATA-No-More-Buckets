import { useEffect, useState } from "react";
import { ArrowRight, Check, CircleAlert, Cloud, Droplets, Gauge, Image, LockKeyhole, Mic, Play, RotateCcw, ShieldCheck, Sparkles, Wrench } from "lucide-react";
import { api, Incident, RuntimeMetadata } from "./api";

const demoSteps = [
  { action: "seed", label: "Tenant report", icon: Droplets },
  { action: "eta", label: "ETA notification", icon: Gauge },
  { action: "work_started", label: "Vendor check-in", icon: Wrench },
  { action: "completion", label: "Verify completion", icon: ShieldCheck },
  { action: "tenant_confirm", label: "Delayed confirmation", icon: Check },
];

function statusLabel(status: string) {
  return status.replaceAll("_", " ").toLowerCase();
}

function timelineLabel(kind: string) {
  return kind.replaceAll("_", " ");
}

function App() {
  const [incident, setIncident] = useState<Incident | null>(null);
  const [runtime, setRuntime] = useState<RuntimeMetadata | null>(null);
  const [step, setStep] = useState(-1);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { api.runtime().then(setRuntime).catch((err: Error) => setError(err.message)); }, []);

  async function startDemo() {
    setError(""); setRunning(true); setStep(0);
    try {
      let current = await api.seed();
      setIncident(current);
      // Demo mode compresses the same workflow; the backend still records every event and gate.
      await new Promise((resolve) => setTimeout(resolve, 450));
      current = await api.action(current.incident_id, "eta"); setIncident(current); setStep(1);
      await new Promise((resolve) => setTimeout(resolve, 450));
      current = await api.action(current.incident_id, "work_started"); setIncident(current); setStep(2);
      await new Promise((resolve) => setTimeout(resolve, 450));
      const photo = { asset_id: "media-completion-photo", filename: "after-repair.jpg", mime_type: "image/jpeg", size_bytes: 15, sha256: "", content_base64: "c3ludGhldGljLWltYWdl", source: "vendor" };
      current = await api.action(current.incident_id, "completion", {
        photo: { ...photo, sha256: await digest("synthetic-image") },
        invoice: {
          invoice_id: "invoice-demo-001", vendor_id: current.assigned_vendor_id ?? "vendor-b",
          currency: "SGD", total: 220,
          line_items: [{ description: "leak repair labor and parts", quantity: 1, unit_price: 220 }],
        },
      }); setIncident(current); setStep(3);
      await new Promise((resolve) => setTimeout(resolve, 450));
      current = await api.action(current.incident_id, "tenant_confirm"); setIncident(current); setStep(4);
    } catch (err) { setError(err instanceof Error ? err.message : "Demo failed"); }
    finally { setRunning(false); }
  }

  async function digest(value: string) {
    const bytes = new TextEncoder().encode(value);
    const hash = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(hash)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function reset() {
    setIncident(null); setStep(-1); setError("");
    await fetch("/api/demo/reset", { method: "POST" });
  }

  async function runException() {
    setError("");
    try {
      const current = await api.report({
        property_id: "demo-tampines-101",
        tenant_id: "tenant-demo-exception",
        report_text: "Water is spraying beside a wet electrical outlet and I see sparks.",
        voice_transcript: "This feels dangerous and I cannot safely reach the shutoff.",
        idempotency_key: `exception:${crypto.randomUUID()}`,
      });
      setIncident(current); setStep(-1);
    } catch (err) { setError(err instanceof Error ? err.message : "Exception path failed"); }
  }

  const fallback = incident?.timeline.find((entry) => entry.kind === "vendor_dispatch_outcome" && entry.metadata.outcome !== "accept");
  const accepted = incident?.timeline.find((entry) => entry.kind === "vendor_dispatch_outcome" && entry.metadata.outcome === "accept");

  return (
    <main className="app-shell">
      <nav className="topbar">
        <div className="brand"><div className="brand-mark"><Droplets size={18} /></div><span>no more buckets</span></div>
        <div className="topbar-right"><span className="live-dot" /> DEMO CONTROL ROOM <span className="pill">DEMO CLOCK ENABLED</span><span className="pill">SYNTHETIC DATA</span></div>
      </nav>
      <section className="hero">
        <div className="eyebrow"><Sparkles size={14} /> AUTONOMOUS INCIDENT COORDINATOR</div>
        <h1>From first drip<br /><em>to done.</em></h1>
        <p className="lede">A bounded, auditable workflow that keeps a leak from becoming a landlord-sized problem.</p>
        <div className="hero-actions">
          <button className="primary-button" onClick={startDemo} disabled={running}><Play size={16} fill="currentColor" /> {running ? "Running compressed demo…" : "Run four-minute demo"}</button>
          <button className="ghost-button" onClick={runException} disabled={running}><CircleAlert size={15} /> Try safety exception</button>
          {incident && <button className="ghost-button" onClick={reset}><RotateCcw size={15} /> Reset</button>}
        </div>
        {error && <div className="error-banner"><CircleAlert size={16} /> {error}</div>}
      </section>

      <section className="proof-strip">
        <div><span className="proof-icon"><Cloud size={16} /></span><span><small>EXECUTION</small><strong>{runtime?.deployment ?? "loading"}</strong></span></div>
        <div><span className="proof-icon"><Sparkles size={16} /></span><span><small>OBSERVATION LAYER</small><strong>{runtime?.facts_provider ?? "loading"} · {runtime?.facts_model ?? ""}</strong></span></div>
        <div><span className="proof-icon"><LockKeyhole size={16} /></span><span><small>SOURCE OF TRUTH</small><strong>{runtime?.storage_backend ?? "loading"} · append-only timeline</strong><small className="timing-note">Demo clock enabled · 8s / 12s / 15s / 30s</small></span></div>
      </section>

      <section className="workspace-grid">
        <div className="journey-card panel">
          <div className="panel-heading"><div><span className="kicker">THE JOURNEY</span><h2>One incident, every decision.</h2></div><span className="state-chip">{incident ? statusLabel(incident.status) : "ready"}</span></div>
          <div className="steps">
            {demoSteps.map((item, index) => { const Icon = item.icon; const done = index <= step; return <div className={`journey-step ${done ? "done" : ""} ${index === step ? "current" : ""}`} key={item.action}><div className="step-number">{done ? <Check size={14} /> : index + 1}</div><Icon size={16} /><span>{item.label}</span>{index < demoSteps.length - 1 && <ArrowRight className="step-arrow" size={14} />}</div>; })}
          </div>
          {incident ? <div className="report-preview"><div className="report-top"><span className="kicker">TENANT REPORT · {incident.incident_id}</span><span className="confidence"><Sparkles size={13} /> {Math.round((incident.facts?.source_confidence ?? 0) * 100)}% facts confidence</span></div><p>“{incident.report_text}”</p><div className="media-row"><span><Image size={15} /> photo attached</span><span><Mic size={15} /> voice note transcribed</span><span>{incident.facts?.affected_rooms[0] ?? "fixture"}</span></div></div> : <div className="empty-state"><Droplets size={30} /><p>Press the demo button to watch the full incident lifecycle.</p></div>}
        </div>

        <div className="assessment-card panel">
          <div className="panel-heading"><div><span className="kicker">MULTIMODAL ASSESSMENT</span><h2>Facts first. Policy second.</h2></div><ShieldCheck className="safe-icon" size={22} /></div>
          {incident?.facts ? <div className="facts"><div className="fact-row"><span>Issue type</span><strong>{incident.facts.issue_type}</strong></div><div className="fact-row"><span>Severity</span><strong className={incident.facts.severity === "critical" ? "danger" : "amber"}>{incident.facts.severity}</strong></div><div className="fact-row"><span>Water visible</span><strong>{incident.facts.water_visible ? "yes" : "no"}</strong></div><div className="fact-row"><span>Estimated repair</span><strong>S${incident.facts.estimated_cost?.toFixed(0)} / S$ {incident.work_order?.spending_limit.toFixed(0)} cap</strong></div></div> : <div className="placeholder-lines"><span /><span /><span /><span /></div>}
          {incident?.containment_instructions && <div className="containment"><div className="containment-title"><ShieldCheck size={15} /> CONTAINMENT INSTRUCTIONS SENT</div><p>{incident.containment_instructions}</p></div>}
          <div className="policy-note"><LockKeyhole size={14} /><span>LLM extracts observable facts. Deterministic rules authorize spend, safety, access, dispatch, and closure.</span></div>
        </div>
      </section>

      <section className="lower-grid">
        <div className="timeline-card panel"><div className="panel-heading"><div><span className="kicker">PERSISTED TIMELINE</span><h2>Audit trail, not a chat transcript.</h2></div><span className="event-count">{incident?.timeline.length ?? 0} events</span></div>{incident ? <div className="timeline">{incident.timeline.map((entry) => <div className="timeline-item" key={`${entry.event_id}-${entry.kind}`}><div className="timeline-dot" /><div className="timeline-copy"><div className="timeline-meta"><span>{new Date(entry.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>{entry.rule_id && <code>{entry.rule_id}</code>}</div><strong>{timelineLabel(entry.kind)}</strong>{entry.state_to && <span className="transition">{entry.state_from} → {entry.state_to}</span>}{Boolean(entry.metadata.vendor_id) && <span className="timeline-detail">{String(entry.metadata.vendor_id)} · {String(entry.metadata.outcome ?? "")}</span>}{Boolean(entry.metadata.blocking_reasons) && <span className="timeline-detail danger-text">{String(entry.metadata.blocking_reasons)}</span>}</div></div>)}</div> : <div className="empty-state compact"><LockKeyhole size={24} /><p>Every state transition and tool outcome will appear here.</p></div>}</div>
        <div className="outcome-card panel"><div className="panel-heading"><div><span className="kicker">GUARDRAILS IN ACTION</span><h2>Exceptions stay narrow.</h2></div><Gauge size={21} /></div><div className="guardrail-list"><div className={`guardrail ${fallback ? "active" : ""}`}><span className="guardrail-icon"><CircleAlert size={15} /></span><span><strong>Vendor A failure</strong><small>{fallback ? "Declined · fallback triggered" : "Waiting for demo"}</small></span></div><div className={`guardrail ${accepted ? "active" : ""}`}><span className="guardrail-icon"><ArrowRight size={15} /></span><span><strong>Vendor B fallback</strong><small>{accepted ? "Accepted · late A cannot replace B" : "Bounded vendor ranking"}</small></span></div><div className={`guardrail ${incident?.last_evidence?.passed ? "active" : ""}`}><span className="guardrail-icon"><ShieldCheck size={15} /></span><span><strong>Evidence gate</strong><small>{incident?.last_evidence?.passed ? "Photo + invoice verified" : "Missing or mismatched evidence blocks close"}</small></span></div><div className={`guardrail ${incident?.status === "CLOSED" ? "active" : ""}`}><span className="guardrail-icon"><Check size={15} /></span><span><strong>Final state</strong><small>{incident ? statusLabel(incident.status) : "Not started"}</small></span></div></div><div className="final-state"><span>WORKFLOW STATE</span><strong>{incident ? incident.status : "—"}</strong>{incident?.warranty_expires_at && <small>Warranty through {new Date(incident.warranty_expires_at).toLocaleDateString()}</small>}</div></div>
      </section>

      <footer><span>NO MORE BUCKETS · ATA HACKATHON</span><span>SAFE AUTOMATION FOR SMALL RENTALS</span></footer>
    </main>
  );
}

export default App;
