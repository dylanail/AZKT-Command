/* Contact detail: header · identities (add/remove, verified labels) · linked opportunities / requests / vehicles /
   tasks / commitments · consent · merge history with Unmerge (owner) · Edit, Archive/Restore, Propose merge
   (needs_review explained), Mark provisional, owner-only Merge. GET /api/contacts/{id}; writes via POST actions. */
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api, command, describeError, type CommandResult } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { TZ, relativeTime } from "../../lib/format";
import { useIsMobile } from "../../lib/viewport";
import { Avatar, Badge, Button, Chip, EmptyState, ErrorState, Field, GlassPanel, HealthLabel, Input, Loading, Notice, PageHeader, ResponsiveDialog, Select, Textarea, When, useToast } from "../../ui";
import { ReasonDialog, RescheduleSheet, AssignSheet, taskPath, useTaskCommand, useTaskDialogs } from "../tasks/TaskSheets";
import { TaskRow } from "../tasks/TaskRow";
import { usePeople } from "../tasks/usePeople";
import { vehicleLabel } from "../tasks/useNames";
import { pipelineLabel, stageLabel } from "../sales/types";
import { IDENTITY_KINDS, IDENTITY_LABEL, ROLE_LABEL, ROLE_OPTIONS, identityValue, statusBadge, type Contact, type ContactDetailResp, type ContactListResp } from "./types";
import "./contacts.css";

const cpath = (id: string, action: string) => `/api/contacts/${encodeURIComponent(id)}/${action}`;

/** Shared outcome handling for contact commands. Returns the result when it applied, null otherwise. */
function useContactCommand() {
  const { toast } = useToast();
  const nav = useNavigate();
  return useCallback(async <T,>(path: string, body: Record<string, unknown>, okMessage?: string): Promise<CommandResult<T> | null> => {
    try {
      const res = await command<T>(path, body);
      if (res.status === "needs_review") {
        toast({ title: "Sent for approval", tone: "wait", message: res.decision?.reasons?.[0] || "Nothing changes until the owner approves.", actions: res.approval_id ? [{ label: "Review", primary: true, onClick: () => nav(`/approvals/${res.approval_id}`) }] : undefined });
        return res;
      }
      if (res.status === "blocked") { toast({ message: res.decision?.reasons?.join("; ") || "Blocked", tone: "blocked" }); return null; }
      if (okMessage) toast({ message: okMessage, tone: "ok" });
      return res;
    } catch (e) {
      toast({ message: describeError(e), tone: e instanceof ApiError && e.isBusinessGate ? "risk" : "blocked" });
      return null;
    }
  }, [toast, nav]);
}

export default function ContactDetail() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  const run = useContactCommand();
  const runTask = useTaskCommand();
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const q = useQuery<ContactDetailResp | null>((signal) => api.get<ContactDetailResp | null>(`/api/contacts/${encodeURIComponent(id)}`, { signal, tolerate: [404] }), [id, tick]);
  const write = can(user, "contacts.write");
  const isOwner = user?.role === "owner";
  const people = usePeople(!!user && user.scope === "all");
  const dialogs = useTaskDialogs();
  const [editOpen, setEditOpen] = useState(false);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [provOpen, setProvOpen] = useState(false);
  const [unmergeFor, setUnmergeFor] = useState<string | null>(null);
  const [identBusy, setIdentBusy] = useState<string | null>(null);
  const [ident, setIdent] = useState({ kind: "email", value: "", label: "" });
  const [mergeNotice, setMergeNotice] = useState<{ approvalId: string | null; text: string } | null>(null);

  const d = q.data;
  const c = d?.contact;
  const crumbs = [{ label: "Contacts", to: "/contacts" }, { label: c?.name || `Contact ${id.slice(0, 8)}` }];

  const removeIdentity = async (identityId: string) => {
    if (!c) return;
    setIdentBusy(identityId);
    const res = await run(`/api/contacts/${encodeURIComponent(c.id)}/identities/${encodeURIComponent(identityId)}/remove`, { expected_version: c.version }, "Identity removed");
    setIdentBusy(null);
    if (res?.status === "ok") reload();
  };
  const addIdentity = async (e: FormEvent) => {
    e.preventDefault();
    if (!c || !ident.value.trim()) return;
    setIdentBusy("new");
    const res = await run(cpath(c.id, "identities"), { kind: ident.kind, value: ident.value.trim(), label: ident.label.trim(), expected_version: c.version }, "Identity added");
    setIdentBusy(null);
    if (res?.status === "ok") { setIdent({ kind: "email", value: "", label: "" }); reload(); }
  };
  const restore = async () => {
    if (!c) return;
    const res = await run(cpath(c.id, "restore"), { expected_version: c.version }, "Contact restored");
    if (res?.status === "ok") reload();
  };
  const unmerge = async (reason: string) => {
    if (!unmergeFor) return false;
    const res = await run(`/api/contacts/merges/${encodeURIComponent(unmergeFor)}/unmerge`, { reason }, "Merge undone · records went back");
    if (res?.status === "ok") reload();
    return !!res;
  };

  if (q.loading) return <div className="page"><PageHeader title={<span className="skeleton" style={{ display: "inline-block", width: 220, height: 28 }} />} crumbs={crumbs} /><GlassPanel clip><Loading rows={4} label="Loading contact" /></GlassPanel></div>;
  if (q.error) return <div className="page"><PageHeader title="Contact" crumbs={crumbs} /><ErrorState error={q.error} onRetry={q.reload} /></div>;
  if (!d || !c) return <div className="page"><PageHeader title="Contact not found" crumbs={crumbs} /><GlassPanel clip><EmptyState title="Contact not found" body={`Nothing recorded for ${id}.`} action={<Button variant="soft" to="/contacts">Back to contacts</Button>} /></GlassPanel></div>;

  const badge = statusBadge(c);
  const archived = c.status === "archived";
  const merged = c.status === "merged";
  const idents = c.identities || [];
  const consentEntries = Object.entries(d.consent || {});
  const lastTouch = d.conversations?.[0]?.last_inbound_at || c.updated_at;

  return (
    <div className="page page-wide">
      <PageHeader
        title={<span className="row" style={{ gap: 12 }}><Avatar name={c.name} /><span>{c.name}</span>{badge ? <Badge tone={badge.tone}>{badge.label}</Badge> : null}</span>}
        crumbs={crumbs}
        subtitle={
          <span className="row-wrap" style={{ gap: 6 }}>
            {c.company ? <span>{c.company} ·</span> : null}
            {(c.roles || []).length ? (c.roles || []).map((r) => <Chip key={r} size="sm" tone="soft">{ROLE_LABEL[r] || r}</Chip>) : <span className="not-recorded">No roles</span>}
            {c.verified ? <HealthLabel health="ok" label="Verified" /> : null}
            <span>· {lastTouch ? `last touch ${relativeTime(lastTouch)}` : "no activity yet"}</span>
            {c.source ? <span>· from {c.source}</span> : null}
          </span>
        }
        actions={
          <div className="cd-actions">
            {merged ? null : archived ? (
              <Button variant="primary" onClick={restore} disabled={!write} disabledReason={whyNot("contacts.write")}>Restore</Button>
            ) : (
              <>
                <Button variant="glass" onClick={() => setEditOpen(true)} disabled={!write} disabledReason={whyNot("contacts.write")}>Edit</Button>
                <Button variant="glass" onClick={() => setMergeOpen(true)} disabled={!write} disabledReason={whyNot("contacts.write")}>{isOwner ? "Merge" : "Propose merge"}</Button>
                <Button variant="soft" onClick={() => setProvOpen(true)} disabled={!write} disabledReason={whyNot("contacts.write")}>{c.status === "provisional" ? "Confirm as active" : "Mark provisional"}</Button>
                <Button variant="ghost" onClick={() => setArchiveOpen(true)} disabled={!write} disabledReason={whyNot("contacts.write")}>Archive</Button>
              </>
            )}
          </div>
        }
      />

      {merged && d.merged_into ? <Notice tone="wait" lead="Merged" action={<Button size="sm" variant="primary" to={`/contacts/${encodeURIComponent(d.merged_into.id)}`}>Open surviving contact</Button>}>This record was merged{c.merged_at ? <> <When iso={c.merged_at} format="date" /></> : null}. Its identities and links now live on the survivor.</Notice> : null}
      {archived ? <Notice tone="risk" lead="Archived">{c.archived_at ? <When iso={c.archived_at} format="long" /> : null}. Restore to link new leads or messages.</Notice> : null}
      {c.status === "provisional" ? <Notice tone="wait" lead="Provisional">{c.provisional_reason || "Created from an unknown low-risk sender; confirm once you know who this is."}</Notice> : null}
      {mergeNotice ? <Notice tone="wait" lead="Merge proposed" action={mergeNotice.approvalId ? <Button size="sm" variant="primary" to={`/approvals/${encodeURIComponent(mergeNotice.approvalId)}`}>Review</Button> : undefined}>{mergeNotice.text}</Notice> : null}

      <div className="cd-grid">
        <GlassPanel clip>
          <section className="cd-section">
            <div className="cd-section__title"><span>Identities</span><span className="count">{idents.length}</span></div>
            {idents.length === 0 ? <span className="not-recorded">No identities recorded.</span> : idents.map((i) => (
              <div key={i.id} className="cd-item">
                <div className="cd-item__main">
                  <span>{identityValue(i)}{i.is_primary ? <Badge tone="soft" style={{ marginLeft: 6 }}>Primary</Badge> : null}</span>
                  <span className="cd-item__meta">{IDENTITY_LABEL[i.kind] || i.kind}{i.label ? ` · ${i.label}` : ""}{i.source ? ` · ${i.source}` : ""}</span>
                </div>
                <div className="cd-item__right">
                  {i.verified ? <HealthLabel health="ok" label="Verified" /> : <span className="fs12 t4">Unverified</span>}
                  <Button size="xs" variant="ghost" loading={identBusy === i.id} onClick={() => removeIdentity(i.id)} disabled={!write || merged} disabledReason={merged ? "Merged records are read-only." : whyNot("contacts.write")}>Remove</Button>
                </div>
              </div>
            ))}
            {write && !merged && !archived ? (
              <form className="cd-ident-form" onSubmit={addIdentity} style={{ marginTop: 6 }}>
                <Select value={ident.kind} onChange={(e) => setIdent({ ...ident, kind: e.target.value })} aria-label="Identity kind">
                  {IDENTITY_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
                </Select>
                <Input value={ident.value} onChange={(e) => setIdent({ ...ident, value: e.target.value })} placeholder={ident.kind === "email" ? "name@…" : ident.kind === "phone" ? "+1 …" : "Handle or ref"} aria-label="Identity value" />
                <Input value={ident.label} onChange={(e) => setIdent({ ...ident, label: e.target.value })} placeholder="Label" aria-label="Identity label" />
                <Button type="submit" size="md" variant="soft" loading={identBusy === "new"} disabled={!ident.value.trim()} disabledReason="Type the identity first.">Add</Button>
              </form>
            ) : null}
            <span className="fs12 t4">Manually entered identities count as verified. Quoted or forwarded text never does.</span>
          </section>
        </GlassPanel>

        <GlassPanel clip>
          <section className="cd-section">
            <div className="cd-section__title"><span>Consent</span></div>
            {consentEntries.length === 0 ? <span className="not-recorded">No consent recorded. Sending stays owner-approved.</span> : consentEntries.map(([k, v]) => (
              <div key={k} className="cd-item"><span style={{ textTransform: "capitalize" }}>{k.replace(/_/g, " ")}</span><span className="fs13" style={{ color: v === true ? "var(--ok)" : v === false ? "var(--blocked)" : "var(--t3)" }}>{v === true ? "Yes" : v === false ? "No" : String(v)}</span></div>
            ))}
            {c.notes ? <><div className="cd-section__title" style={{ marginTop: 8 }}><span>Notes</span></div><div className="td-body fs13">{c.notes}</div></> : null}
            {(c.aliases || []).length ? <><div className="cd-section__title" style={{ marginTop: 8 }}><span>Also known as</span></div><span className="fs13 t2">{(c.aliases || []).map((a) => [a.name, a.company].filter(Boolean).join(" · ")).filter(Boolean).join("; ")}</span></> : null}
          </section>
        </GlassPanel>

        <GlassPanel clip>
          <section className="cd-section">
            <div className="cd-section__title"><span>Opportunities</span><span className="count">{d.opportunities.length}</span></div>
            {d.opportunities.length === 0 ? <span className="not-recorded">No leads yet.</span> : d.opportunities.map((o) => (
              <div key={o.id} className="cd-item">
                <div className="cd-item__main">
                  <Link to={`/sales?lead=${encodeURIComponent(o.id)}&pipeline=${o.pipeline === "irq" ? "irq" : "vehicle"}`}>{pipelineLabel(o.pipeline)}{o.enquiry ? ` · ${o.enquiry}` : o.vehicle_id ? ` · vehicle ${o.vehicle_id.slice(0, 8)}` : ""}</Link>
                  <span className="cd-item__meta">{o.created_at ? `opened ${relativeTime(o.created_at)}` : ""}{o.lost_reason ? ` · ${o.lost_reason}` : ""}</span>
                </div>
                <Chip size="sm" tone={o.stage === "deposit_paid" ? "ok" : o.stage === "lost" ? "soft" : "neutral"}>{o.stage_label || stageLabel(o.stage)}</Chip>
              </div>
            ))}
            {d.import_requests.length ? <div className="cd-section__title" style={{ marginTop: 8 }}><span>Import requests</span><span className="count">{d.import_requests.length}</span></div> : null}
            {d.import_requests.map((r) => (
              <div key={r.id} className="cd-item">
                <div className="cd-item__main"><Link to={`/requests/${encodeURIComponent(r.id)}`}>{r.title || `Request ${r.id.slice(0, 8)}`}</Link><span className="cd-item__meta">{[r.status, r.deposit_status ? `deposit ${r.deposit_status}` : null].filter(Boolean).join(" · ")}</span></div>
              </div>
            ))}
            {d.vehicles.length ? <div className="cd-section__title" style={{ marginTop: 8 }}><span>Vehicles</span><span className="count">{d.vehicles.length}</span></div> : null}
            {d.vehicles.map((v) => (
              <div key={v.id} className="cd-item">
                <div className="cd-item__main"><Link to={`/vehicles/${encodeURIComponent(v.id)}`}>{vehicleLabel(v, v.id)}</Link><span className="cd-item__meta">{[v.commercial_state, v.allocation].filter(Boolean).join(" · ")}</span></div>
              </div>
            ))}
            {d.conversations?.length ? <div className="cd-section__title" style={{ marginTop: 8 }}><span>Threads</span><span className="count">{d.conversations.length}</span></div> : null}
            {(d.conversations || []).slice(0, 5).map((cv) => (
              <div key={cv.id} className="cd-item">
                <div className="cd-item__main"><Link to={`/inbox/${encodeURIComponent(cv.id)}`}>{cv.subject || "(no subject)"}</Link><span className="cd-item__meta">{[cv.channel, cv.state, cv.last_inbound_at ? relativeTime(cv.last_inbound_at) : null].filter(Boolean).join(" · ")}</span></div>
              </div>
            ))}
          </section>
        </GlassPanel>

        <GlassPanel clip>
          <section className="cd-section">
            <div className="cd-section__title"><span>Promises</span><span className="count">{d.promises.length}</span></div>
            {d.promises.length === 0 ? <span className="not-recorded">Nothing promised on record.</span> : d.promises.map((p) => {
              const over = p.status === "open" && p.due_at && new Date(p.due_at).getTime() < Date.now();
              return (
                <div key={p.id} className="cd-item">
                  <div className="cd-item__main">
                    <span>{p.text}</span>
                    <span className="cd-item__meta">{p.made_at ? `made ${relativeTime(p.made_at)}` : ""}{p.made_by ? ` · ${people.nameOf(p.made_by)}` : ""}</span>
                  </div>
                  <span className="fs13" style={{ color: over ? "var(--blocked)" : p.status === "open" ? "var(--t2)" : "var(--ok)", whiteSpace: "nowrap" }}>
                    {p.status !== "open" ? (p.status || "done") : p.due_at ? <>{over ? "Overdue · " : "due "}<When iso={p.due_at} tz={TZ.phoenix} /></> : "no due date"}
                  </span>
                </div>
              );
            })}
          </section>
        </GlassPanel>
      </div>

      <section>
        <div className="section-title"><h2>Tasks <span className="count">{d.tasks.length}</span></h2>{can(user, "tasks.write") && !merged ? <Button size="sm" variant="soft" to={`/tasks?new=1`}>New task</Button> : null}</div>
        <GlassPanel>
          {d.tasks.length === 0 ? <EmptyState title="No tasks for this contact" body="Calls, meetings and follow-ups from their leads show here." /> : d.tasks.map((t) => (
            <TaskRow key={t.id} task={t} ownerName={people.nameOf(t.owner_user_id)} dialogs={dialogs} onChanged={() => reload()}
              about={t.opportunity_id ? { label: "Open lead", to: `/sales?lead=${encodeURIComponent(t.opportunity_id)}` } : t.vehicle_id ? { label: `Vehicle ${t.vehicle_id.slice(0, 8)}`, to: `/vehicles/${encodeURIComponent(t.vehicle_id)}` } : null} />
          ))}
        </GlassPanel>
      </section>

      <section>
        <div className="section-title"><h2>Merge history <span className="count">{d.merge_history.length}</span></h2></div>
        <GlassPanel clip>
          {d.merge_history.length === 0 ? <EmptyState title="Never merged" body="Duplicates merge only with review and can be undone here." /> : (
            <div className="cd-section">
              {d.merge_history.map((m) => {
                const survivorHere = m.survivor_id === c.id;
                return (
                  <div key={m.id} className="cd-item">
                    <div className="cd-item__main">
                      <span>{survivorHere ? <>Absorbed <Link to={`/contacts/${encodeURIComponent(m.merged_id)}`}>contact {m.merged_id.slice(0, 8)}</Link></> : <>Merged into <Link to={`/contacts/${encodeURIComponent(m.survivor_id)}`}>contact {m.survivor_id.slice(0, 8)}</Link></>}{m.reverted_at ? " · undone" : ""}</span>
                      <span className="cd-item__meta">{m.created_at ? <When iso={m.created_at} format="long" /> : null}{m.merged_by ? ` · ${people.nameOf(m.merged_by)}` : ""}{m.reason ? ` · ${m.reason}` : ""}{m.approval_id ? <> · <Link to={`/approvals/${encodeURIComponent(m.approval_id)}`}>approval</Link></> : null}</span>
                    </div>
                    {!m.reverted_at ? <Button size="xs" variant="soft" onClick={() => setUnmergeFor(m.id)} disabled={!isOwner} disabledReason="Only the owner undoes a merge.">Unmerge</Button> : <span className="fs12 t4">Reverted {m.reverted_at ? relativeTime(m.reverted_at) : ""}</span>}
                  </div>
                );
              })}
            </div>
          )}
        </GlassPanel>
      </section>

      <EditContactDialog contact={c} open={editOpen} onClose={() => setEditOpen(false)} onSaved={reload} />
      <MergeDialog contact={c} open={mergeOpen} onClose={() => setMergeOpen(false)} isOwner={isOwner}
        onDone={(r) => { setMergeOpen(false); if (r.status === "needs_review") setMergeNotice({ approvalId: r.approval_id, text: "Sent to the owner for review. Nothing changes until it's approved; the proposal is re-checked at approval time." }); else { setMergeNotice(null); reload(); } }} />
      <ReasonDialog open={archiveOpen} onClose={() => setArchiveOpen(false)} title="Archive contact" description={c.name} label="Why (optional)" confirmLabel="Archive" tone="danger"
        onConfirm={async (reason) => { const res = await run(cpath(c.id, "archive"), { reason, expected_version: c.version }, "Archived · pending approvals for this contact are invalidated"); if (res?.status === "ok") reload(); return !!res; }} />
      <ReasonDialog open={provOpen} onClose={() => setProvOpen(false)} title={c.status === "provisional" ? "Confirm as active" : "Mark provisional"} description={c.name} label={c.status === "provisional" ? "Note (optional)" : "Why is this uncertain?"} confirmLabel={c.status === "provisional" ? "Confirm" : "Mark provisional"}
        onConfirm={async (reason) => { const res = await run(cpath(c.id, "mark-provisional"), { provisional: c.status !== "provisional", reason: reason || undefined, expected_version: c.version }, c.status === "provisional" ? "Confirmed as active" : "Marked provisional"); if (res?.status === "ok") reload(); return !!res; }} />
      <ReasonDialog open={!!unmergeFor} onClose={() => setUnmergeFor(null)} title="Undo this merge" description="Rows and identities go back to the other contact; aliases are restored." label="Why (optional)" confirmLabel="Unmerge" tone="danger" onConfirm={unmerge} />
      <RescheduleSheet task={dialogs.reschedule} open={!!dialogs.reschedule} onClose={() => dialogs.setReschedule(null)} onSaved={() => reload()} />
      <AssignSheet task={dialogs.assign} open={!!dialogs.assign} onClose={() => dialogs.setAssign(null)} onSaved={() => reload()} />
      <ReasonDialog open={!!dialogs.cancel} onClose={() => dialogs.setCancel(null)} title="Cancel task" description={dialogs.cancel?.title} label="Why (optional)" confirmLabel="Cancel task" tone="danger"
        onConfirm={async (reason) => { const t = dialogs.cancel; if (!t) return false; const out = await runTask(taskPath(t.id, "cancel"), { reason: reason || undefined, expected_version: t.version }, { okMessage: "Cancelled" }); if (out.result?.status === "ok") reload(); return !out.error; }} />
      <ReasonDialog open={!!dialogs.reject} onClose={() => dialogs.setReject(null)} title="Reject evidence" description={dialogs.reject?.title} label="What's missing or wrong" confirmLabel="Reject and reopen" required tone="danger"
        onConfirm={async (reason) => { const t = dialogs.reject; if (!t) return false; const out = await runTask(taskPath(t.id, "reject"), { reason, expected_version: t.version }, { okMessage: "Reopened with your reason" }); if (out.result?.status === "ok") reload(); return !out.error; }} />
    </div>
  );
}

/* ---------- Edit ---------- */
function EditContactDialog({ contact: c, open, onClose, onSaved }: { contact: Contact; open: boolean; onClose: () => void; onSaved: () => void }) {
  const mobile = useIsMobile();
  const run = useContactCommand();
  const [name, setName] = useState(c.name);
  const [company, setCompany] = useState(c.company || "");
  const [roles, setRoles] = useState<string[]>(c.roles || []);
  const [notes, setNotes] = useState(c.notes || "");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setName(c.name); setCompany(c.company || ""); setRoles(c.roles || []); setNotes(c.notes || ""); } }, [open, c]);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    const res = await run(cpath(c.id, "update"), { name: name.trim(), company: company.trim() || null, roles, notes, expected_version: c.version }, "Saved");
    setBusy(false);
    if (res?.status === "ok") { onSaved(); onClose(); }
  };
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="Edit contact"
      footer={<><Button type="submit" form="ct-edit" variant="primary" loading={busy} disabled={!name.trim()} disabledReason="A name is required.">Save</Button><Button variant="ghost" onClick={onClose}>Cancel</Button></>}>
      <form id="ct-edit" className="stack" onSubmit={submit}>
        <div className="tk-grid2">
          <Field label="Name" required><Input value={name} onChange={(e) => setName(e.target.value)} maxLength={200} /></Field>
          <Field label="Company"><Input value={company} onChange={(e) => setCompany(e.target.value)} /></Field>
        </div>
        <div className="field">
          <span className="field__label"><span>Roles</span></span>
          <div className="cd-roles" role="group" aria-label="Roles">
            {ROLE_OPTIONS.map((r) => <Chip key={r.id} size="sm" tone={roles.includes(r.id) ? "act" : "neutral"} selected={roles.includes(r.id)} onClick={() => setRoles((xs) => (xs.includes(r.id) ? xs.filter((x) => x !== r.id) : [...xs, r.id]))}>{r.label}</Chip>)}
          </div>
        </div>
        <Field label="Notes"><Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- Merge (propose, or owner merge with confirmation) ---------- */
function MergeDialog({ contact: c, open, onClose, isOwner, onDone }: { contact: Contact; open: boolean; onClose: () => void; isOwner: boolean; onDone: (r: CommandResult<unknown>) => void }) {
  const mobile = useIsMobile();
  const run = useContactCommand();
  const [search, setSearch] = useState("");
  const [results, setResults] = useState<Contact[]>([]);
  const [searching, setSearching] = useState(false);
  const [pick, setPick] = useState<Contact | null>(null);
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setSearch(""); setResults([]); setPick(null); setReason(""); setConfirm(""); } }, [open]);
  useEffect(() => {
    if (!open) return;
    const s = search.trim();
    if (!s) { setResults([]); return; }
    const ctrl = new AbortController();
    const h = window.setTimeout(() => {
      setSearching(true);
      api.get<ContactListResp | null>(`/api/contacts?tab=all&limit=20&q=${encodeURIComponent(s)}`, { signal: ctrl.signal, tolerate: [404] })
        .then((r) => { if (!ctrl.signal.aborted) setResults((r?.items || []).filter((x) => x.id !== c.id)); })
        .catch(() => { /* typing continues */ })
        .finally(() => { if (!ctrl.signal.aborted) setSearching(false); });
    }, 250);
    return () => { window.clearTimeout(h); ctrl.abort(); };
  }, [search, open, c.id]);

  const merged = pick;
  const summary = useMemo(() => merged ? `Merge ${merged.name} into ${c.name}. ${c.name} survives; ${merged.name}'s identities, leads, tasks and threads move here and its name is kept as an alias.` : "", [merged, c.name]);
  const submit = async () => {
    if (!merged) return;
    setBusy(true);
    const action = isOwner ? "merge" : "merge-propose";
    const res = await run(cpath(c.id, action), { merged_id: merged.id, reason, expected_survivor_version: c.version, expected_merged_version: merged.version }, isOwner ? "Merged · undo from Merge history" : undefined);
    setBusy(false);
    if (res) onDone(res);
  };
  const needConfirm = isOwner && confirm.trim().toLowerCase() !== "merge";
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title={isOwner ? "Merge contacts" : "Propose a merge"} description={isOwner ? "Audited and undoable. The other contact becomes an alias of this one." : "Merges run only under the owner's exact approval. Nothing changes until then."}
      footer={<><Button variant={isOwner ? "danger" : "primary"} loading={busy} onClick={submit} disabled={!merged || needConfirm} disabledReason={!merged ? "Pick the duplicate first." : "Type MERGE to confirm."}>{isOwner ? "Merge now" : "Send for review"}</Button><Button variant="ghost" onClick={onClose}>Cancel</Button></>}>
      <div className="stack">
        <Field label="Find the duplicate" hint="Search by name, company, email or phone.">
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Who is this a duplicate of?" autoFocus />
        </Field>
        {searching ? <Loading rows={1} /> : results.length ? (
          <div className="ct-picker" role="radiogroup" aria-label="Duplicate contact">
            {results.map((r) => (
              <button key={r.id} type="button" role="radio" aria-checked={pick?.id === r.id} className="ct-picker__row" onClick={() => setPick(r)}>
                <span className="cd-item__main"><span>{r.name}{r.company ? <span className="t3"> · {r.company}</span> : null}</span><span className="cd-item__meta">{[r.primary_email, r.primary_phone, r.status !== "active" ? r.status : null].filter(Boolean).join(" · ")}</span></span>
                <span className="fs12 t4">{(r.roles || []).map((x) => ROLE_LABEL[x] || x).join(", ")}</span>
              </button>
            ))}
          </div>
        ) : search.trim() ? <span className="fs13 t4">No other contacts match.</span> : null}
        {merged ? <Notice tone="wait" lead="What will happen">{summary}</Notice> : null}
        <Field label="Reason" hint="Kept in the audit trail and shown to the owner."><Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. same phone, replied from work email" /></Field>
        {isOwner ? <Field label="Type MERGE to confirm" required><Input value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder="MERGE" autoComplete="off" /></Field> : null}
      </div>
    </ResponsiveDialog>
  );
}
