import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api, command, describeError, type CommandResult } from "../../../lib/api";
import { useQuery } from "../../../lib/useQuery";
import { Button, Chip, EmptyState, ErrorState, Field, GlassPanel, Input, Loading, Notice, Select, Textarea, When, useToast } from "../../../ui";
import { ROLE_META, type AgentProfile, type AgentProfileContent, type AgentProfileVersion, type AgentRole, type OpenClawImportAgent } from "../types";

const blank: AgentProfileContent = {
  mission: "", voice: "", core_rules: [], operating_instructions: "", reporting_expectations: "",
  escalation_rules: [], tool_guidance: "", schedule_guidance: "",
};

function lines(v: string[]): string { return (v || []).join("\n"); }
function list(v: string): string[] { return v.split("\n").map((x) => x.replace(/^\s*[-*]\s*/, "").trim()).filter(Boolean); }

async function zipRequest<T>(path: string, file: File): Promise<T> {
  const r = await fetch(path, { method: "POST", credentials: "include", headers: { Accept: "application/json", "Content-Type": "application/zip" }, body: file });
  let body: unknown = null;
  try { body = await r.json(); } catch { /* plain error */ }
  if (!r.ok) {
    const o = body && typeof body === "object" ? body as Record<string, unknown> : {};
    throw new Error(String(o.message || o.detail || `Import failed (${r.status}).`));
  }
  return body as T;
}

function statusTone(stage: string): "ok" | "wait" | "soft" {
  return stage === "active" ? "ok" : stage === "validated" ? "wait" : "soft";
}

export function Instructions({ role, owner, onDirty }: { role: AgentRole; owner: boolean; onDirty: (dirty: boolean) => void }) {
  const { toast } = useToast();
  const q = useQuery<AgentProfile>((signal) => api.get(`/api/agent-profiles/${role}`, { signal }), [role]);
  const [form, setForm] = useState<AgentProfileContent>(blank);
  const [rules, setRules] = useState("");
  const [escalations, setEscalations] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [sources, setSources] = useState<OpenClawImportAgent[]>([]);
  const [sourceAgent, setSourceAgent] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [baseline, setBaseline] = useState<AgentProfileContent>(blank);
  const initialized = useRef(false);

  const loadContent = (content: AgentProfileContent, id: string | null) => {
    setSelectedId(id);
    setBaseline(content);
    setForm({ ...blank, ...content });
    setRules(lines(content.core_rules));
    setEscalations(lines(content.escalation_rules));
    setNote("");
  };

  useEffect(() => {
    if (!q.data || initialized.current) return;
    initialized.current = true;
    loadContent(q.data.effective_content, q.data.current_version_id);
  }, [q.data, role]);

  const selected = q.data?.versions.find((v) => v.id === selectedId);
  const dirty = useMemo(() => {
    if (!q.data) return false;
    const next = { ...form, core_rules: list(rules), escalation_rules: list(escalations) };
    return JSON.stringify(next) !== JSON.stringify(baseline) || !!note.trim();
  }, [form, rules, escalations, note, q.data, baseline]);

  useEffect(() => {
    onDirty(dirty);
  }, [dirty, onDirty]);

  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const openVersion = (v: AgentProfileVersion) => {
    if (dirty && !window.confirm("Discard your unsaved edits and open this saved version?")) return;
    loadContent(v.content, v.id);
  };

  const patch = <K extends keyof AgentProfileContent>(key: K, value: AgentProfileContent[K]) => setForm((s) => ({ ...s, [key]: value }));

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!owner || !form.mission.trim()) return;
    setBusy("save");
    try {
      const r = await command<{ profile: AgentProfile; version: AgentProfileVersion }>(`/api/agent-profiles/${role}/versions`, {
        ...form, core_rules: list(rules), escalation_rules: list(escalations), change_note: note.trim(), source_kind: "manual",
      });
      if (r.status === "ok") {
        if (r.data?.version) loadContent(r.data.version.content, r.data.version.id);
        toast({ message: r.data?.version ? `Draft version ${r.data.version.version_no} saved.` : "Draft saved.", tone: "ok" });
        q.reload();
      }
    } catch (err) { toast({ message: describeError(err), tone: "blocked" }); }
    finally { setBusy(""); }
  };

  const validate = async (version: AgentProfileVersion) => {
    setBusy(`validate:${version.id}`);
    try {
      const r = await command<{ validation: { ok: boolean; failures: string[] } }>(`/api/agent-profile-versions/${version.id}/validate`, {});
      if (r.status === "ok") toast({ message: r.data?.validation.ok ? "Instruction checks passed. Review any warnings before activation." : `Checks failed: ${r.data?.validation.failures.join(" ")}`, tone: r.data?.validation.ok ? "ok" : "blocked" });
      q.reload();
    } catch (err) { toast({ message: describeError(err), tone: "blocked" }); }
    finally { setBusy(""); }
  };

  const activate = async (version: AgentProfileVersion) => {
    setBusy(`activate:${version.id}`);
    try {
      const r = await command(`/api/agent-profile-versions/${version.id}/activate`, { reason: "Activated from the Agents page" });
      if (r.status === "ok") toast({ message: `Version ${version.version_no} is now live for new ${ROLE_META[role].label} work.`, tone: "ok" });
      else if (r.status === "needs_review") toast({ message: "Activation is prepared for your review; nothing changed yet.", tone: "wait" });
      q.reload();
    } catch (err) { toast({ message: describeError(err), tone: "blocked" }); }
    finally { setBusy(""); }
  };

  const chooseFile = async (next: File | null) => {
    setFile(next); setSources([]); setSourceAgent("");
    if (!next) return;
    setBusy("preview");
    try {
      const preview = await zipRequest<{ agents: OpenClawImportAgent[] }>("/api/agent-profiles/import/preview", next);
      setSources(preview.agents);
      const preferred: Partial<Record<AgentRole, string>> = { manager: "architect", sourcing: "scout", listings: "watcher" };
      const first = preview.agents.find((a) => a.source_agent === preferred[role]) || preview.agents[0];
      setSourceAgent(first?.source_agent || "");
    } catch (err) { toast({ message: describeError(err), tone: "blocked" }); setFile(null); if (fileRef.current) fileRef.current.value = ""; }
    finally { setBusy(""); }
  };

  const importBundle = async () => {
    if (!file || !sourceAgent) return;
    if (dirty && !window.confirm("Discard your unsaved edits and open the imported draft?")) return;
    setBusy("import");
    try {
      const path = `/api/agent-profiles/${role}/import?source_agent=${encodeURIComponent(sourceAgent)}&filename=${encodeURIComponent(file.name)}`;
      const r = await zipRequest<CommandResult<{ version: AgentProfileVersion }>>(path, file);
      if (r.status !== "ok" || !r.data?.version) throw new Error("Import did not save a draft.");
      loadContent(r.data.version.content, r.data.version.id);
      toast({ message: `Imported ${sourceAgent} as draft version ${r.data?.version.version_no || ""}. Review it before activation.`, tone: "ok", duration: 7000 });
      q.reload();
    } catch (err) { toast({ message: describeError(err), tone: "blocked" }); }
    finally { setBusy(""); }
  };

  if (q.loading) return <Loading label="Loading agent instructions" rows={4} />;
  if (q.error) return <ErrorState title="Couldn't load these instructions" error={q.error} onRetry={q.reload} />;
  if (!q.data) return <EmptyState title="Instructions unavailable" body="This role is still using AZKT's built-in defaults." />;

  return (
    <div className="ag-instructions stack-lg">
      <Notice tone="neutral" lead={q.data.current_version ? `Live version ${q.data.current_version.version_no}` : "Built-in defaults are live"}>
        These instructions shape how this role works. AZKT's safety checks, tools and permissions remain fixed outside this editor.
      </Notice>

      <GlassPanel padded>
        <form className="stack" onSubmit={save}>
          <div className="between"><div><h3 className="ag-section-title">Role instructions</h3><div className="fs13 t3">Editing {selected ? `version ${selected.version_no} (${selected.stage})` : "built-in defaults"}. Saving creates a new version.</div></div><Chip size="sm" tone={dirty ? "wait" : "soft"}>{dirty ? "Unsaved changes" : "Saved content"}</Chip></div>
          <details><summary>Compare editor with live instructions</summary>{(Object.keys(blank) as Array<keyof AgentProfileContent>).filter((k) => JSON.stringify(q.data!.effective_content[k]) !== JSON.stringify(({ ...form, core_rules: list(rules), escalation_rules: list(escalations) })[k])).map((k) => <div className="stack" key={k}><strong>{k.replace(/_/g, " ")}</strong><div className="form-grid"><div>Live<pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(q.data!.effective_content[k], null, 2)}</pre></div><div>Editor<pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(({ ...form, core_rules: list(rules), escalation_rules: list(escalations) })[k], null, 2)}</pre></div></div></div>)}</details>
          <Field label="Mission" required hint="The outcome this role owns. This does not grant access to records or tools.">
            <Textarea rows={4} value={form.mission} onChange={(e) => patch("mission", e.target.value)} />
          </Field>
          <Field label="Voice and working style"><Textarea rows={4} value={form.voice} onChange={(e) => patch("voice", e.target.value)} /></Field>
          <Field label="Core business rules" hint="One rule per line. Stable safety and approval rules cannot be replaced here.">
            <Textarea rows={5} value={rules} onChange={(e) => setRules(e.target.value)} placeholder="Never promise a date that a carrier has not confirmed." />
          </Field>
          <Field label="Operating instructions" hint="The detailed playbook for this role. Imported AGENTS.md content appears here.">
            <Textarea rows={10} value={form.operating_instructions} onChange={(e) => patch("operating_instructions", e.target.value)} />
          </Field>
          <div className="form-grid">
            <Field label="Reporting expectations"><Textarea rows={5} value={form.reporting_expectations} onChange={(e) => patch("reporting_expectations", e.target.value)} /></Field>
            <Field label="Escalation rules" hint="One condition per line."><Textarea rows={5} value={escalations} onChange={(e) => setEscalations(e.target.value)} /></Field>
          </div>
          <div className="form-grid">
            <Field label="Tool guidance" hint="Usage notes only—this cannot enable a tool."><Textarea rows={6} value={form.tool_guidance} onChange={(e) => patch("tool_guidance", e.target.value)} /></Field>
            <Field label="Schedule guidance" hint="Planning notes only—this does not create a schedule."><Textarea rows={6} value={form.schedule_guidance} onChange={(e) => patch("schedule_guidance", e.target.value)} /></Field>
          </div>
          <Field label="What changed" hint="Optional note shown in version history."><Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Imported the Scout rules and removed the old Notion steps" /></Field>
          <div className="row-wrap">
            <Button type="submit" variant="primary" size="md" loading={busy === "save"} disabled={!owner || !dirty || !form.mission.trim()} disabledReason={!owner ? "Only the owner can edit agent instructions." : !dirty ? "Nothing has changed." : "A mission is required."}>Save as new draft</Button>
            <span className="fs12 t3">Saving never changes the live version. Check and activate the draft below.</span>
          </div>
        </form>
      </GlassPanel>

      {owner ? <GlassPanel padded>
        <div className="stack">
          <div><h3 className="ag-section-title">Import OpenClaw instructions</h3><div className="fs13 t3">Only IDENTITY.md, SOUL.md, AGENTS.md, TOOLS.md and HEARTBEAT.md are read. Memory, scripts and permission settings are excluded. Common token patterns are redacted; inspect the text for other secrets before activation.</div></div>
          <Field label="OpenClaw agents ZIP"><Input ref={fileRef} type="file" accept=".zip,application/zip" onChange={(e) => void chooseFile(e.target.files?.[0] || null)} /></Field>
          {sources.length ? <div className="form-grid">
            <Field label="Source agent"><Select value={sourceAgent} onChange={(e) => setSourceAgent(e.target.value)}>{sources.map((a) => <option key={a.source_agent} value={a.source_agent}>{a.source_agent} · {a.documents.length} instruction files</option>)}</Select></Field>
            <div className="ag-import-summary fs13 t3">{sources.find((a) => a.source_agent === sourceAgent)?.documents.join(" · ")}</div>
          </div> : null}
          <div className="row-wrap"><Button variant="soft" size="md" loading={busy === "preview" || busy === "import"} disabled={!file || !sourceAgent} disabledReason="Choose a ZIP and source agent first." onClick={() => void importBundle()}>Import into {ROLE_META[role].label} draft</Button><span className="fs12 t3">Imported content is reviewed and activated exactly like a manual edit.</span></div>
        </div>
      </GlassPanel> : null}

      <div className="stack">
        <div><h3 className="ag-section-title">Version history</h3><div className="fs13 t3">Every draft and activation stays visible. Activating an older version is a rollback with a receipt.</div></div>
        <GlassPanel clip>
          {!q.data.versions.length ? <EmptyState title="No saved versions" body="This role is using the built-in AZKT instructions. Save a draft or import an OpenClaw agent to begin." /> : q.data.versions.map((v) => (
            <div className="ag-version" key={v.id}>
              <div className="grow min0"><div className="row-wrap"><strong>Version {v.version_no}</strong><Chip size="sm" tone={statusTone(v.stage)}>{v.stage}</Chip><Chip size="sm" tone="soft">{v.source_kind.replace(/_/g, " ")}</Chip></div><div className="fs13 t3">{v.change_note || "No change note."}{v.created_at ? <> · saved <When iso={v.created_at} relative /></> : null}</div>{v.validation.failures?.length ? <div className="fs12 risk">{v.validation.failures.join(" ")}</div> : v.validation.warnings?.length ? <div className="fs12 t3">{v.validation.warnings.join(" ")}</div> : null}</div>
              <div className="fs12 t3">Saved by {v.created_by || "unknown"}{v.activated_by ? ` · activated by ${v.activated_by}` : ""}</div>
              {owner ? <div className="row-wrap nowrap"><Button size="sm" variant="soft" disabled={!!busy} onClick={() => openVersion(v)}>Open / edit</Button><Button size="sm" variant="soft" loading={busy === `validate:${v.id}`} disabled={!!busy || dirty} onClick={() => void validate(v)}>Check</Button><Button size="sm" variant={v.stage === "active" ? "ok" : "primary"} loading={busy === `activate:${v.id}`} disabled={!!busy || dirty || selectedId !== v.id || v.stage === "active" || !v.validation.ok} disabledReason="Open and review this saved version, save any edits, then run checks before activation." onClick={() => void activate(v)}>{v.stage === "superseded" ? "Restore" : "Activate"}</Button></div> : null}
            </div>
          ))}
        </GlassPanel>
      </div>
    </div>
  );
}
