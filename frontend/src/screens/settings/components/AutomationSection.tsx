/* Settings › Automation & permissions (owner). Pause controls: GET/POST /api/settings/pause (global + per workflow,
   with pending/unknown external action counts shown before pausing). Caps: GET/POST /api/settings/automation.
   Shop gate rules: GET /api/settings/gate-rules, POST upsert, POST /{id}/deactivate, POST /reset. */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, api } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useQuery } from "../../../lib/useQuery";
import { useCommand } from "../../../lib/useCommand";
import { useIsMobile } from "../../../lib/viewport";
import { can, whyNot } from "../../../lib/perms";
import { humanize } from "../../../lib/links";
import { Button, Chip, EmptyState, ErrorState, Field, GlassPanel, Input, Loading, Notice, ResponsiveDialog, Select, Switch, Table, When } from "../../../ui";
import { COST_CATEGORIES, RECON_STATES, REQUIREMENT_LABELS, type AutomationSettings, type GateRule, type GateRulesResp, type PauseResp, type ReportingSettings, type SettingEntry } from "./types";

function OutstandingChips({ o }: { o: PauseResp["outstanding"] }) {
  const x = o.external_actions, a = o.approvals;
  return (
    <div className="row-wrap" style={{ gap: 6 }}>
      <Chip size="sm" tone={x.pending ? "wait" : "soft"} count={x.pending}>pending external</Chip>
      <Chip size="sm" tone={x.executing ? "wait" : "soft"} count={x.executing}>executing</Chip>
      <Chip size="sm" tone={x.unknown ? "risk" : "soft"} count={x.unknown}>result unknown</Chip>
      <Chip size="sm" tone={a.pending ? "amber" : "soft"} count={a.pending}>awaiting decision</Chip>
      <Chip size="sm" tone={a.queued ? "wait" : "soft"} count={a.queued}>approved, not confirmed</Chip>
    </div>
  );
}

function PauseControls() {
  const { run, busy } = useCommand();
  const isMobile = useIsMobile();
  const q = useQuery<PauseResp>((signal) => api.get<PauseResp>("/api/settings/pause", { signal }), []);
  const [confirm, setConfirm] = useState<{ key: string; paused: boolean } | null>(null);
  const [reason, setReason] = useState("");
  const [newKey, setNewKey] = useState("");
  const reasonRef = useRef<HTMLInputElement>(null);

  const apply = async () => {
    if (!confirm) return;
    const r = await run(`pause:${confirm.key}`, "/api/settings/pause", { key: confirm.key, paused: confirm.paused, reason: reason.trim() || null }, {
      success: confirm.paused ? `Paused ${confirm.key === "global" ? "all automation" : confirm.key}. Queued work waits; nothing is undone.` : `Resumed ${confirm.key === "global" ? "automation" : confirm.key}. Queued work is revalidated before it runs.`,
    });
    if (r?.status === "ok") { setConfirm(null); setReason(""); q.reload(); }
  };
  const addWorkflow = (e: FormEvent) => {
    e.preventDefault();
    const k = newKey.trim();
    if (!k) return;
    setConfirm({ key: k.startsWith("workflow:") ? k : `workflow:${k}`, paused: true });
    setNewKey("");
  };

  if (q.loading) return <GlassPanel clip><Loading label="Loading pause controls" rows={2} /></GlassPanel>;
  if (q.error) return <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;
  const d = q.data;
  if (!d) return null;
  const globalCtl = d.items.find((c) => c.key === "global");
  const others = d.items.filter((c) => c.key !== "global");
  return (
    <div className="stack">
      {d.any_paused ? <Notice tone="risk" lead="Automation is paused" role="alert">{d.items.filter((c) => c.paused).map((c) => c.key).join(", ")} · agents don't act until you resume.</Notice> : null}
      <GlassPanel clip>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Pause all automation</span>
            <span className="set-row__meta">{globalCtl?.paused ? <>Paused <When iso={globalCtl.changed_at} relative />{globalCtl.reason ? ` · ${globalCtl.reason}` : ""}</> : "Agents propose and execute within policy. Pausing stops new actions at the next boundary."}</span>
          </div>
          <div className="set-row__right"><Switch checked={!!globalCtl?.paused} onChange={(on) => setConfirm({ key: "global", paused: on })} label={<span className="sr-only">Pause all automation</span>} style={{ width: "auto" }} /></div>
        </div>
        {others.map((c) => (
          <div key={c.key} className="set-row">
            <div className="set-row__main">
              <span className="set-row__title">{humanize(c.key.replace(/^workflow:/, "").replace(/^thread:/, "thread "))}</span>
              <span className="set-row__meta">{c.key}{c.paused ? <> · paused <When iso={c.changed_at} relative />{c.reason ? ` · ${c.reason}` : ""}</> : " · running"}</span>
            </div>
            <div className="set-row__right"><Switch checked={c.paused} onChange={(on) => setConfirm({ key: c.key, paused: on })} label={<span className="sr-only">Pause {c.key}</span>} style={{ width: "auto" }} /></div>
          </div>
        ))}
        <form className="set-row" onSubmit={addWorkflow}>
          <div className="set-row__main"><span className="set-row__title">Pause one workflow</span><span className="set-row__meta">By command name, e.g. inbox.send or quotes.forward_to_customer.</span></div>
          <div className="set-row__right"><Input value={newKey} onChange={(e) => setNewKey(e.target.value)} placeholder="workflow name" aria-label="Workflow to pause" /><Button type="submit" size="sm" variant="soft" disabled={!newKey.trim()} disabledReason="Type a workflow name first.">Pause</Button></div>
        </form>
      </GlassPanel>
      <div className="stack-sm">
        <div className="eyebrow">In flight right now</div>
        <OutstandingChips o={d.outstanding} />
        <div className="set-foot">Pausing never undoes what already reached a provider. Unknown results are reconciled by reference, never retried blindly. Resuming revalidates queued approvals; expired or changed ones stay invalid.</div>
      </div>

      <ResponsiveDialog mobile={isMobile} open={!!confirm} onClose={() => setConfirm(null)} title={confirm ? `${confirm.paused ? "Pause" : "Resume"} ${confirm.key === "global" ? "all automation" : confirm.key}?` : ""} size="sm" initialFocusRef={reasonRef as React.RefObject<HTMLElement>}
        footer={<><Button variant={confirm?.paused ? "danger" : "primary"} loading={confirm ? busy(`pause:${confirm.key}`) : false} onClick={apply}>{confirm?.paused ? "Pause" : "Resume"}</Button><Button variant="ghost" onClick={() => setConfirm(null)}>Cancel</Button></>}>
        <div className="stack">
          <div className="fs14 t2">{confirm?.paused ? "New actions stop at the next boundary. Work already sent to a provider completes on its own; nothing is rolled back." : "Queued work is revalidated against current facts, versions and permissions before anything runs. Nothing is replayed."}</div>
          <OutstandingChips o={d.outstanding} />
          <Field label="Reason (recorded in Activity)"><Input ref={reasonRef} value={reason} onChange={(e) => setReason(e.target.value)} placeholder={confirm?.paused ? "e.g. checking a wrong quote" : "e.g. verified, back to normal"} /></Field>
        </div>
      </ResponsiveDialog>
    </div>
  );
}

function Caps() {
  const { run, busy } = useCommand();
  const q = useQuery<SettingEntry<AutomationSettings>>((signal) => api.get<SettingEntry<AutomationSettings>>("/api/settings/automation", { signal }), []);
  const [v, setV] = useState<AutomationSettings | null>(null);
  useEffect(() => { if (q.data) setV(JSON.parse(JSON.stringify(q.data.value)) as AutomationSettings); }, [q.data]);
  if (q.loading) return <GlassPanel clip><Loading label="Loading caps" rows={2} /></GlassPanel>;
  if (q.error) return <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;
  if (!q.data || !v) return null;
  const dirty = JSON.stringify(v) !== JSON.stringify(q.data.value);
  const money = (s: string | null) => (s === null || s === "" ? "" : s);
  const setMoney = (k: "parts_cap" | "model_daily_budget_usd" | "model_monthly_budget_usd", s: string) => setV((x) => (x ? { ...x, [k]: s.trim() === "" ? null : s.trim() } : x));
  const setCap = (k: "per_action" | "daily" | "monthly", s: string) => setV((x) => (x ? { ...x, spend_caps: { ...x.spend_caps, [k]: s.trim() === "" ? null : s.trim() } } : x));
  const save = async () => {
    const r = await run("automation", "/api/settings/automation", { value: v, expected_version: q.data?.version, replace: true }, { success: "Caps saved.", onError: (_m, e) => { if (e instanceof ApiError && e.code === "conflict") q.reload(); } });
    if (r?.status === "ok") q.reload();
  };
  const cur = v.spend_caps.currency || "USD";
  return (
    <div className="stack">
      <div className="eyebrow">Caps <span style={{ textTransform: "none", letterSpacing: 0 }}>· v{q.data.version}</span></div>
      <GlassPanel clip>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Auto-approve parts orders under</span><span className="set-row__meta">{v.parts_cap === null ? "Unset: every parts order waits for your exact approval (the day-one default)." : `Orders at or above ${v.parts_cap} ${cur} still need your approval.`}</span></div>
          <div className="set-row__right"><Input inputMode="decimal" aria-label="Parts cap" value={money(v.parts_cap)} onChange={(e) => setMoney("parts_cap", e.target.value)} placeholder="unset" style={{ width: 120, minWidth: 0 }} /><span className="t3 fs13">{cur}</span></div>
        </div>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Spend caps for standing permissions</span><span className="set-row__meta">Reserved atomically before execution; simultaneous runs cannot exceed them. Blank = no cap, which means no autonomy.</span></div>
          <div className="set-row__right">
            <Input inputMode="decimal" aria-label="Per action" value={money(v.spend_caps.per_action)} onChange={(e) => setCap("per_action", e.target.value)} placeholder="per action" style={{ width: 110, minWidth: 0 }} />
            <Input inputMode="decimal" aria-label="Per day" value={money(v.spend_caps.daily)} onChange={(e) => setCap("daily", e.target.value)} placeholder="per day" style={{ width: 110, minWidth: 0 }} />
            <Input inputMode="decimal" aria-label="Per month" value={money(v.spend_caps.monthly)} onChange={(e) => setCap("monthly", e.target.value)} placeholder="per month" style={{ width: 110, minWidth: 0 }} />
            <Input aria-label="Currency" value={cur} maxLength={3} onChange={(e) => setV((x) => (x ? { ...x, spend_caps: { ...x.spend_caps, currency: e.target.value.toUpperCase() } } : x))} style={{ width: 70, minWidth: 0 }} />
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Model budget</span><span className="set-row__meta">Drafting and summaries stop when a budget is hit; deterministic lists keep working. Usage shows spend.</span></div>
          <div className="set-row__right">
            <Input inputMode="decimal" aria-label="Daily model budget USD" value={money(v.model_daily_budget_usd)} onChange={(e) => setMoney("model_daily_budget_usd", e.target.value)} placeholder="daily USD" style={{ width: 110, minWidth: 0 }} />
            <Input inputMode="decimal" aria-label="Monthly model budget USD" value={money(v.model_monthly_budget_usd)} onChange={(e) => setMoney("model_monthly_budget_usd", e.target.value)} placeholder="monthly USD" style={{ width: 120, minWidth: 0 }} />
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Discretionary AI</span><span className="set-row__meta">Let agents pick up improvement work on their own within policy. Off keeps them reactive.</span></div>
          <div className="set-row__right"><Switch checked={v.discretionary_ai_enabled} onChange={(on) => setV((x) => (x ? { ...x, discretionary_ai_enabled: on } : x))} label={<span className="sr-only">Discretionary AI</span>} style={{ width: "auto" }} /></div>
        </div>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Who verifies finished shop work</span><span className="set-row__meta">Only the owner in this version. Send customer messages and publish always need approval until a specific standing permission exists.</span></div>
          <div className="set-row__right"><Button size="sm" variant="soft" to="/settings/team">Team &amp; permissions</Button></div>
        </div>
      </GlassPanel>
      <div className="row-wrap">
        <Button variant="primary" loading={busy("automation")} disabled={!dirty} disabledReason="Nothing changed yet." onClick={save}>Save caps</Button>
        <Button variant="ghost" disabled={!dirty} disabledReason="Nothing to discard." onClick={() => setV(JSON.parse(JSON.stringify(q.data?.value)) as AutomationSettings)}>Discard</Button>
      </div>
    </div>
  );
}

/* Reporting — which cost categories must be recorded before Home labels gross profit "Recorded".
   GET/POST /api/settings/reporting, the same envelope the caps block uses for /api/settings/automation. */
function Reporting() {
  const { run, busy } = useCommand();
  const q = useQuery<SettingEntry<ReportingSettings>>((signal) => api.get<SettingEntry<ReportingSettings>>("/api/settings/reporting", { signal }), []);
  const [v, setV] = useState<ReportingSettings | null>(null);
  useEffect(() => { if (q.data) setV(JSON.parse(JSON.stringify(q.data.value)) as ReportingSettings); }, [q.data]);
  if (q.loading) return <GlassPanel clip><Loading label="Loading reporting" rows={2} /></GlassPanel>;
  if (q.error) return <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;
  if (!q.data || !v) return null;

  const required = v.required_cost_categories || [];
  const dirty = JSON.stringify(required) !== JSON.stringify(q.data.value.required_cost_categories || []);
  const toggle = (value: string, on: boolean) => setV((x) => {
    if (!x) return x;
    const keep = new Set(x.required_cost_categories || []);
    if (on) keep.add(value); else keep.delete(value);
    return { ...x, required_cost_categories: COST_CATEGORIES.map((c) => c.value).filter((c) => keep.has(c)) };
  });
  const save = async () => {
    const r = await run("reporting", "/api/settings/reporting", { value: v, expected_version: q.data?.version, replace: true },
      { success: "Reporting saved.", onError: (_m, e) => { if (e instanceof ApiError && e.code === "conflict") q.reload(); } });
    if (r?.status === "ok") q.reload();
  };

  return (
    <div className="stack">
      <div className="eyebrow">Reporting <span style={{ textTransform: "none", letterSpacing: 0 }}>· v{q.data.version}</span></div>
      <GlassPanel clip>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Costs that must be recorded</span>
            <span className="set-row__meta">Gross profit is labelled Recorded only when these costs are recorded for every vehicle in the period.</span>
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main">
            <div className="row-wrap" style={{ gap: 14 }}>
              {COST_CATEGORIES.map((c) => (
                <label key={c.value} className="row" style={{ gap: 8, minHeight: 44 }}>
                  <input type="checkbox" checked={required.includes(c.value)} onChange={(e) => toggle(c.value, e.target.checked)} />
                  <span>{c.label}</span>
                </label>
              ))}
            </div>
          </div>
        </div>
      </GlassPanel>
      <div className="row-wrap">
        <Button variant="primary" loading={busy("reporting")} disabled={!dirty || required.length === 0}
          disabledReason={required.length === 0 ? "Pick at least one cost category." : "Nothing changed yet."} onClick={save}>Save reporting</Button>
        <Button variant="ghost" disabled={!dirty} disabledReason="Nothing to discard."
          onClick={() => setV(JSON.parse(JSON.stringify(q.data?.value)) as ReportingSettings)}>Discard</Button>
      </div>
    </div>
  );
}

function GateRules() {
  const { run, busy } = useCommand();
  const isMobile = useIsMobile();
  const q = useQuery<GateRulesResp>((signal) => api.get<GateRulesResp>("/api/settings/gate-rules", { signal }), []);
  const [edit, setEdit] = useState<Partial<GateRule> | null>(null);
  const firstRef = useRef<HTMLSelectElement>(null);
  const factual = new Set(q.data?.factual || []);
  const requirements = q.data?.requirements || Object.keys(REQUIREMENT_LABELS);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!edit?.to_state || !edit.requirement) return;
    const body: Record<string, unknown> = {
      rule_id: edit.id || null, expected_version: edit.id ? edit.version : null, to_state: edit.to_state, requirement: edit.requirement,
      label: (edit.label || "").trim() || null, param: edit.requirement === "photos_min" ? { min: Number((edit.param as { min?: unknown })?.min) || 6 } : {},
      overridable: !!edit.overridable && !factual.has(edit.requirement), active: edit.active !== false,
    };
    const r = await run("gate", "/api/settings/gate-rules", body, { success: "Gate rule saved.", onError: (_m, err) => { if (err instanceof ApiError && err.code === "conflict") q.reload(); } });
    if (r?.status === "ok") { setEdit(null); q.reload(); }
  };
  const deactivate = async (r: GateRule) => {
    if (!r.id) return;
    const res = await run(`deact:${r.id}`, `/api/settings/gate-rules/${encodeURIComponent(r.id)}/deactivate`, { expected_version: r.version }, { success: `"${r.label}" no longer gates ${RECON_STATES.find((s) => s.value === r.to_state)?.label || r.to_state}.` });
    if (res?.status === "ok") q.reload();
  };
  const reset = async () => {
    const res = await run("reset", "/api/settings/gate-rules/reset", {}, { success: "Default gate rules restored." });
    if (res?.status === "ok") q.reload();
  };

  return (
    <div className="stack">
      <div className="between">
        <div className="eyebrow">Shop gate rules</div>
        <div className="row">
          <Button size="sm" variant="soft" onClick={() => setEdit({ to_state: "ready_for_sale", requirement: "disclosures_written", param: {}, overridable: false, active: true })}>Add rule</Button>
          <Button size="sm" variant="ghost" loading={busy("reset")} onClick={reset}>Reset to defaults</Button>
        </div>
      </div>
      <GlassPanel clip>
        {q.loading ? <Loading label="Loading gate rules" rows={3} /> : q.error ? <ErrorState error={q.error} onRetry={q.reload} /> : !(q.data?.items || []).length ? (
          <EmptyState title="No gate rules" body="Vehicles can move stages freely. Reset to defaults to restore the spec's gates." />
        ) : (
          <Table minWidth={640} aria-label="Gate rules">
            <thead><tr><th>Moving to</th><th>Requires</th><th>Override</th><th>Source</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {(q.data?.items || []).map((r, i) => (
                <tr key={r.id || `${r.to_state}:${r.requirement}:${i}`}>
                  <td>{RECON_STATES.find((s) => s.value === r.to_state)?.label || humanize(r.to_state)}</td>
                  <td><span className="stack-sm" style={{ gap: 0 }}><span>{r.label}</span><span className="fs12 t3">{REQUIREMENT_LABELS[r.requirement] || humanize(r.requirement)}{r.requirement === "photos_min" && r.param?.min !== undefined ? ` · min ${String(r.param.min)}` : ""}</span></span></td>
                  <td>{r.factual ? <span className="t3">Never · factual</span> : r.overridable ? <span style={{ color: "var(--risk)" }}>With reason</span> : "No"}</td>
                  <td className="t3">{r.source === "default" ? "Default" : <>Saved{r.updated_at ? <> · <When iso={r.updated_at} format="date" /></> : null}</>}</td>
                  <td className="actions">
                    <span className="row" style={{ justifyContent: "flex-end" }}>
                      <Button size="xs" variant="soft" onClick={() => setEdit({ ...r })}>Edit</Button>
                      <Button size="xs" variant="ghost" loading={!!r.id && busy(`deact:${r.id}`)} disabled={!r.id} disabledReason="Defaults aren't stored; save it first, then deactivate." onClick={() => deactivate(r)}>Deactivate</Button>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </GlassPanel>
      <div className="set-foot">Factual gates — photos, verified recon, inspection logged, documents complete — describe physical evidence and are never overridable: no role, agent or reason turns them green. Only "Disclosures written" may be made overridable, and every override is recorded with a reason.</div>

      <ResponsiveDialog mobile={isMobile} open={!!edit} onClose={() => setEdit(null)} title={edit?.id ? "Edit gate rule" : "Add gate rule"} size="sm" initialFocusRef={firstRef as React.RefObject<HTMLElement>}
        footer={<><Button type="submit" form="gate-form" variant="primary" loading={busy("gate")}>Save</Button><Button variant="ghost" onClick={() => setEdit(null)}>Cancel</Button></>}>
        {edit ? (
          <form id="gate-form" className="stack" onSubmit={save}>
            <Field label="When moving to">
              <Select ref={firstRef} value={edit.to_state || ""} onChange={(e) => setEdit((x) => ({ ...x, to_state: e.target.value }))}>{RECON_STATES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}</Select>
            </Field>
            <Field label="Require">
              <Select value={edit.requirement || ""} onChange={(e) => setEdit((x) => ({ ...x, requirement: e.target.value, overridable: factual.has(e.target.value) ? false : x?.overridable }))}>{requirements.map((r) => <option key={r} value={r}>{REQUIREMENT_LABELS[r] || humanize(r)}</option>)}</Select>
            </Field>
            {edit.requirement === "photos_min" ? <Field label="Minimum photos"><Input type="number" min={1} value={String((edit.param as { min?: unknown })?.min ?? 6)} onChange={(e) => setEdit((x) => ({ ...x, param: { min: Number(e.target.value) || 1 } }))} /></Field> : null}
            <Field label="Label" hint="Shown on the vehicle when the gate blocks a move."><Input value={edit.label || ""} onChange={(e) => setEdit((x) => ({ ...x, label: e.target.value }))} placeholder={REQUIREMENT_LABELS[edit.requirement || ""] || ""} /></Field>
            <Switch checked={!!edit.overridable && !factual.has(edit.requirement || "")} onChange={(on) => setEdit((x) => ({ ...x, overridable: on }))} label="Overridable with a reason" disabled={factual.has(edit.requirement || "")} disabledReason="Factual gates are not overridable." />
            <Switch checked={edit.active !== false} onChange={(on) => setEdit((x) => ({ ...x, active: on }))} label="Active" />
          </form>
        ) : null}
      </ResponsiveDialog>
    </div>
  );
}

export function AutomationSection() {
  const { user } = useAuth();
  if (!can(user, "settings")) {
    return <GlassPanel clip><EmptyState title="Owner only" body={`${whyNot("settings")} Pause controls, caps and gate rules live here.`} /></GlassPanel>;
  }
  return (
    <div className="stack-lg">
      <PauseControls />
      <Caps />
      <Reporting />
      <GateRules />
    </div>
  );
}
