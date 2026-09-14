/* Contacts: Buyers | Vendors | Exporters | Carriers | All from GET /api/contacts?tab=&q=; rows with name, company,
   primary identity, roles, provisional/merged badges and last activity; New contact → POST /api/contacts. */
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, api, command, describeError } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { relativeTime } from "../../lib/format";
import { useIsMobile } from "../../lib/viewport";
import { Avatar, Badge, Button, Chip, EmptyState, ErrorState, Field, GlassPanel, Input, ListRow, Loading, Notice, PageHeader, PlusIcon, ResponsiveDialog, Select, Tabs, Textarea, useToast } from "../../ui";
import { IDENTITY_KINDS, ROLE_LABEL, ROLE_OPTIONS, TABS, primaryIdentity, statusBadge, type Contact, type ContactListResp, type ContactTab } from "./types";
import "./contacts.css";

const isTab = (s: string | null): s is ContactTab => !!s && TABS.some((t) => t.id === s);

export default function Contacts() {
  const { user } = useAuth();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") || "";
  const tab: ContactTab = isTab(params.get("tab")) ? (params.get("tab") as ContactTab) : "all";
  const [search, setSearch] = useState(q);
  const [newOpen, setNewOpen] = useState(params.get("new") === "1");
  const write = can(user, "contacts.write");
  const setParam = (k: string, v: string | null) => { const p = new URLSearchParams(params); if (v) p.set(k, v); else p.delete(k); setParams(p, { replace: true }); };

  useEffect(() => { setSearch(q); }, [q]);
  useEffect(() => {
    const h = window.setTimeout(() => { if (search.trim() !== q) setParam("q", search.trim() || null); }, 300);
    return () => window.clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const list = useQuery<ContactListResp | null>((signal) => api.get<ContactListResp | null>(`/api/contacts?tab=${tab}&limit=200${q ? `&q=${encodeURIComponent(q)}` : ""}`, { signal, tolerate: [404, 501] }), [tab, q]);
  const counts = useQuery<Record<string, number>>(async (signal) => {
    const out: Record<string, number> = {};
    await Promise.all(TABS.map(async (t) => {
      const r = await api.get<ContactListResp | null>(`/api/contacts?tab=${t.id}&limit=1${q ? `&q=${encodeURIComponent(q)}` : ""}`, { signal, tolerate: [403, 404, 501] }).catch(() => null);
      if (r) out[t.id] = r.total;
    }));
    return out;
  }, [q]);

  const rows = list.data?.items || [];
  const subtitle = q ? `Search: "${q}"` : counts.data && counts.data.all !== undefined ? `${counts.data.all} contacts · buyers, vendors, exporters and carriers, with what each was promised.` : "Buyers, vendors, exporters and carriers, with what each was promised.";

  return (
    <div className="page">
      <PageHeader title="Contacts" subtitle={subtitle}
        actions={<Button variant="primary" iconLeft={<PlusIcon />} onClick={() => setNewOpen(true)} disabled={!write} disabledReason={whyNot("contacts.write")}>Add contact</Button>}>
        <div className="ct-toolbar">
          <Tabs<ContactTab> label="Contact type" value={tab} onChange={(t) => setParam("tab", t === "all" ? null : t)} tabs={TABS.map((t) => ({ id: t.id, label: t.label, count: counts.data?.[t.id] }))} />
          <Input className="ct-search" pill value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name, company, email, phone…" aria-label="Search contacts" />
        </div>
      </PageHeader>

      <GlassPanel clip>
        {list.loading ? <Loading label="Loading contacts" rows={4} />
          : list.error ? <ErrorState error={list.error} onRetry={list.reload} />
          : list.data === null ? <EmptyState title="Contacts aren't connected yet" body="This list fills in once the contacts API is live." />
          : rows.length === 0 ? <EmptyState title={q ? "No contacts match" : "No contacts yet"} body={q ? "Try a name, company, email or phone." : "Add one, or they appear as leads and messages come in."} action={write && !q ? <Button size="sm" variant="soft" onClick={() => setNewOpen(true)}>Add contact</Button> : undefined} />
          : rows.map((c) => <ContactRow key={c.id} c={c} />)}
      </GlassPanel>
      {list.data && list.data.total > rows.length ? <span className="fs12 t4">Showing {rows.length} of {list.data.total}. Narrow with search.</span> : null}

      <NewContactDialog open={newOpen} onClose={() => { setNewOpen(false); if (params.get("new")) setParam("new", null); }} onCreated={(c) => { list.reload(); counts.reload(); nav(`/contacts/${encodeURIComponent(c.id)}`); }} />
    </div>
  );
}

function ContactRow({ c }: { c: Contact }) {
  const badge = statusBadge(c);
  const ident = primaryIdentity(c);
  const meta = [c.company, ident, c.updated_at ? `last activity ${relativeTime(c.updated_at)}` : null].filter(Boolean).join(" · ");
  return (
    <ListRow
      to={`/contacts/${encodeURIComponent(c.id)}`}
      leading={<Avatar name={c.name} />}
      title={c.name}
      tags={badge ? <Badge tone={badge.tone}>{badge.label}</Badge> : null}
      meta={meta || <span className="not-recorded">No details recorded</span>}
      right={<span className="ct-row__right">{(c.roles || []).slice(0, 3).map((r) => <Chip key={r} size="sm" tone="soft">{ROLE_LABEL[r] || r}</Chip>)}</span>}
      chevron
    />
  );
}

/* ---------- New contact ---------- */
interface IdentityDraft { kind: string; value: string; label: string; }
export function NewContactDialog({ open, onClose, onCreated, defaultRoles = [] }: { open: boolean; onClose: () => void; onCreated: (c: Contact) => void; defaultRoles?: string[] }) {
  const mobile = useIsMobile();
  const { toast } = useToast();
  const [name, setName] = useState("");
  const [company, setCompany] = useState("");
  const [roles, setRoles] = useState<string[]>(defaultRoles);
  const [idents, setIdents] = useState<IdentityDraft[]>([{ kind: "email", value: "", label: "" }]);
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  useEffect(() => { if (open) { setName(""); setCompany(""); setRoles(defaultRoles); setIdents([{ kind: "email", value: "", label: "" }]); setNotes(""); setErr(null); } }, [open, defaultRoles]);

  const toggleRole = (r: string) => setRoles((xs) => (xs.includes(r) ? xs.filter((x) => x !== r) : [...xs, r]));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const res = await command<{ contact?: Contact; created?: boolean }>("/api/contacts", {
        name: name.trim(), company: company.trim() || null, roles, notes,
        identities: idents.filter((i) => i.value.trim()).map((i, n) => ({ kind: i.kind, value: i.value.trim(), label: i.label.trim(), is_primary: n === 0 })),
        source: "manual",
      });
      if (res.status === "needs_review") { toast({ title: "Sent for approval", message: "The owner reviews this first.", tone: "wait" }); onClose(); return; }
      if (res.status === "blocked") { setErr(res.decision?.reasons?.join("; ") || "Blocked"); return; }
      const c = res.data?.contact;
      if (!c) { setErr("Saved, but no contact came back. Refresh the list."); return; }
      toast({ message: res.data?.created === false ? `${c.name} already exists — opened the existing contact.` : `${c.name} added`, tone: res.data?.created === false ? "wait" : "ok" });
      onCreated(c);
      onClose();
    } catch (ex) {
      setErr(ex instanceof ApiError && ex.code === "validation_failed" ? ex.message : describeError(ex));
    } finally { setBusy(false); }
  };
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="New contact" initialFocusRef={nameRef}
      footer={<><Button type="submit" form="ct-new" variant="primary" loading={busy} disabled={!name.trim()} disabledReason="Add a name first.">Add contact</Button><Button variant="ghost" onClick={onClose}>Cancel</Button><span className="fs12 t4">Known identities never duplicate a person.</span></>}>
      <form id="ct-new" className="stack" onSubmit={submit}>
        {err ? <Notice tone="blocked" lead="Not saved" role="alert">{err}</Notice> : null}
        <div className="tk-grid2">
          <Field label="Name" required><Input ref={nameRef} value={name} onChange={(e) => setName(e.target.value)} placeholder="Full name" maxLength={200} /></Field>
          <Field label="Company"><Input value={company} onChange={(e) => setCompany(e.target.value)} placeholder="Optional" /></Field>
        </div>
        <div className="field">
          <span className="field__label"><span>Roles</span></span>
          <div className="cd-roles" role="group" aria-label="Roles">
            {ROLE_OPTIONS.map((r) => <Chip key={r.id} size="sm" tone={roles.includes(r.id) ? "act" : "neutral"} selected={roles.includes(r.id)} onClick={() => toggleRole(r.id)}>{r.label}</Chip>)}
          </div>
        </div>
        <div className="field">
          <span className="field__label"><span>Identities</span><button type="button" className="linklike fs12" onClick={() => setIdents((xs) => [...xs, { kind: "phone", value: "", label: "" }])}>+ Add another</button></span>
          <div className="stack-sm">
            {idents.map((i, n) => (
              <div key={n} className="cd-ident-form">
                <Select value={i.kind} onChange={(e) => setIdents((xs) => xs.map((x, j) => (j === n ? { ...x, kind: e.target.value } : x)))} aria-label="Identity kind">
                  {IDENTITY_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
                </Select>
                <Input value={i.value} onChange={(e) => setIdents((xs) => xs.map((x, j) => (j === n ? { ...x, value: e.target.value } : x)))} placeholder={i.kind === "email" ? "name@…" : i.kind === "phone" ? "+1 …" : "Handle or ref"} aria-label="Identity value" />
                <Input value={i.label} onChange={(e) => setIdents((xs) => xs.map((x, j) => (j === n ? { ...x, label: e.target.value } : x)))} placeholder="Label" aria-label="Identity label" />
                <Button size="md" variant="ghost" onClick={() => setIdents((xs) => xs.filter((_, j) => j !== n))} disabled={idents.length === 1} disabledReason="Keep at least one row (leave it empty if unknown).">Remove</Button>
              </div>
            ))}
          </div>
          <span className="field__hint">The first identity becomes primary. Phones default to US unless they start with +.</span>
        </div>
        <Field label="Notes"><Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} placeholder="Optional" /></Field>
      </form>
    </ResponsiveDialog>
  );
}
