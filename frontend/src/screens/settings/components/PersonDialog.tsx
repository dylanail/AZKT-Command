/* Add / Edit person dialog (owner). Invite: POST /api/team/invite → the one-time link is shown exactly once.
   Edit: POST /api/team/{id}/update (changed fields + expected_version), disable/enable, grant/revoke costs.read.
   Owner-locked keys render disabled with "Owner only"; a person can never change their own access. */
import { useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "../../../lib/auth";
import { useCommand } from "../../../lib/useCommand";
import { useIsMobile } from "../../../lib/viewport";
import { OWNER_LOCKED, PERM_GROUPS, PERM_LABELS, ROLE_DEFAULTS, ROLE_DESCRIPTIONS, ROLE_LABELS, effectivePerms, type Perm, type Role } from "../../../lib/perms";
import { Button, Expander, Field, HealthLabel, Input, KeyValues, Notice, ResponsiveDialog, SegmentedControl, Select, Switch, When, useToast } from "../../../ui";
import { contactOf, statusView, type InviteData, type Person } from "./types";

const ROLE_OPTIONS: Role[] = ["manager", "mechanic", "sales", "logistics", "books", "owner"];

export interface PersonDialogProps {
  open: boolean;
  mode: "invite" | "edit";
  person?: Person | null;
  people: Person[];
  onClose: () => void;
  /** Called after any successful write so the list reloads. */
  onChanged: () => void;
}

function splitContact(s: string): { email: string | null; phone: string | null } {
  const v = s.trim();
  if (!v) return { email: null, phone: null };
  return v.includes("@") ? { email: v, phone: null } : { email: null, phone: v };
}

export function PersonDialog({ open, mode, person, people, onClose, onChanged }: PersonDialogProps) {
  const { user } = useAuth();
  const isMobile = useIsMobile();
  const { run, busy } = useCommand();
  const { toast } = useToast();
  const isEdit = mode === "edit" && !!person;
  const self = isEdit && person?.id === user?.id;

  const [name, setName] = useState("");
  const [contact, setContact] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [role, setRole] = useState<Role>("mechanic");
  const [scope, setScope] = useState<"all" | "assigned">("assigned");
  const [managerId, setManagerId] = useState<string>("");
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const [reason, setReason] = useState("");
  const [confirmDisable, setConfirmDisable] = useState(false);
  const [invite, setInvite] = useState<InviteData | null>(null);
  const [copied, setCopied] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setInvite(null); setCopied(false); setConfirmDisable(false); setReason("");
    if (isEdit && person) {
      setName(person.display_name || ""); setEmail(person.email || ""); setPhone(person.phone || ""); setContact("");
      setRole((person.role as Role) || "mechanic"); setScope(person.scope === "assigned" ? "assigned" : "all");
      setManagerId(person.manager_id || ""); setOverrides({ ...(person.overrides || {}) });
    } else {
      setName(""); setContact(""); setEmail(""); setPhone(""); setRole("mechanic"); setScope("assigned");
      setManagerId(user?.id || ""); setOverrides({});
    }
  }, [open, isEdit, person, user?.id]);

  const effective = useMemo(() => effectivePerms(role, overrides), [role, overrides]);
  const defaults = ROLE_DEFAULTS[role] || ROLE_DEFAULTS.mechanic;
  const customCount = Object.keys(overrides).filter((k) => overrides[k] !== defaults[k as Perm] && !(OWNER_LOCKED.has(k) && role !== "owner")).length;
  const onCount = Object.values(effective).filter(Boolean).length;
  const managers = people.filter((p) => p.status === "active" && (p.role === "owner" || p.role === "manager") && p.id !== person?.id);
  const accessLocked = self;
  const accessReason = "You can't change your own role, scope or permissions.";

  const toggle = (k: Perm, on: boolean) => {
    setOverrides((o) => {
      const n = { ...o };
      if (on === defaults[k]) delete n[k]; else n[k] = on;
      return n;
    });
  };
  const changeRole = (r: Role) => {
    setRole(r);
    if (!isEdit) setScope(["owner", "manager", "sales", "logistics", "books"].includes(r) ? "all" : "assigned");
  };

  const save = async () => {
    if (!name.trim()) { toast({ message: "A name is needed.", tone: "risk" }); return; }
    if (!isEdit) {
      const c = splitContact(contact);
      if (!c.email && !c.phone) { toast({ message: "A phone number or email is needed to deliver the invitation.", tone: "risk" }); return; }
      const r = await run<InviteData>("invite", "/api/team/invite", {
        display_name: name.trim(), email: c.email, phone: c.phone, role, scope, manager_id: managerId || null, perms: overrides,
      });
      if (r?.status === "ok" && r.data) { setInvite(r.data); onChanged(); }
      return;
    }
    if (!person) return;
    const patch: Record<string, unknown> = { expected_version: person.version };
    if (name.trim() !== (person.display_name || "")) patch.display_name = name.trim();
    if (email.trim() !== (person.email || "")) patch.email = email.trim();
    if (phone.trim() !== (person.phone || "")) patch.phone = phone.trim();
    if (!accessLocked) {
      if (role !== person.role) patch.role = role;
      if (scope !== person.scope) patch.scope = scope;
      if ((managerId || null) !== (person.manager_id || null)) { if (managerId) patch.manager_id = managerId; else patch.clear_manager = true; }
      if (JSON.stringify(overrides) !== JSON.stringify(person.overrides || {})) patch.perms = overrides;
      if (reason.trim()) patch.reason = reason.trim();
    }
    if (Object.keys(patch).length === 1) { onClose(); return; }
    const r = await run("update", `/api/team/${encodeURIComponent(person.id)}/update`, patch, { success: `Saved. ${patch.role || patch.scope || patch.perms || patch.manager_id || patch.clear_manager ? "Their sessions end; they sign in again with the new access." : ""}` });
    if (r?.status === "ok") { onChanged(); onClose(); }
  };

  const disable = async () => {
    if (!person) return;
    const r = await run("disable", `/api/team/${encodeURIComponent(person.id)}/disable`, { expected_version: person.version, reason: reason.trim() || null }, { success: `${person.display_name} disabled. Their sessions ended and queued approvals were withdrawn.` });
    if (r?.status === "ok") { onChanged(); onClose(); }
  };
  const enable = async () => {
    if (!person) return;
    const r = await run("enable", `/api/team/${encodeURIComponent(person.id)}/enable`, { expected_version: person.version }, { success: `${person.display_name} can sign in again.` });
    if (r?.status === "ok") { onChanged(); onClose(); }
  };
  const grant = async (on: boolean) => {
    if (!person) return;
    const path = `/api/team/${encodeURIComponent(person.id)}/${on ? "grant" : "revoke-grant"}`;
    const r = await run(on ? "grant" : "revoke", path, { perm: "costs.read", expected_version: person.version, note: reason.trim() || null }, { success: on ? "Costs and margins granted. Recorded with your name and time." : "Cost access withdrawn. Their sessions and queued cost approvals ended." });
    if (r?.status === "ok") { onChanged(); onClose(); }
  };

  const inviteUrl = invite?.token ? `${window.location.origin}/invite/${invite.token}` : null;
  const copy = async () => {
    if (!inviteUrl) return;
    try { await navigator.clipboard.writeText(inviteUrl); setCopied(true); toast({ message: "Link copied.", tone: "ok" }); }
    catch { toast({ message: "Couldn't copy — select the link and copy it by hand.", tone: "risk" }); }
  };

  const title = invite ? "Invitation ready" : isEdit ? `Edit ${person?.display_name || "person"}` : "Add person";
  const sv = person ? statusView(person.status) : null;

  const footer = invite ? (
    <>
      <Button variant="primary" onClick={onClose}>Done</Button>
      <span className="fs13 t3">Nothing changes until they accept.</span>
    </>
  ) : confirmDisable ? (
    <>
      <Button variant="danger" loading={busy("disable")} onClick={disable}>Disable access</Button>
      <Button variant="ghost" onClick={() => setConfirmDisable(false)}>Keep access</Button>
      <span className="fs13 t3">Their sessions end now; queued approvals are withdrawn.</span>
    </>
  ) : (
    <>
      <Button variant="primary" loading={busy("invite") || busy("update")} onClick={save}>{isEdit ? "Save changes" : "Send invitation"}</Button>
      <Button variant="ghost" onClick={onClose}>Cancel</Button>
      {!isEdit ? <span className="fs13 t3">They get a link by text or email. Nothing changes until they accept.</span> : null}
    </>
  );

  return (
    <ResponsiveDialog mobile={isMobile} open={open} onClose={onClose} title={title} size="lg" align="top" footer={footer} initialFocusRef={nameRef as React.RefObject<HTMLElement>}>
      {invite ? (
        <div className="stack">
          {inviteUrl ? (
            <>
              <div>{invite.invitation.display_name} joins as {ROLE_LABELS[invite.invitation.role as Role] || invite.invitation.role}. This link is shown <b style={{ fontWeight: 600 }}>once</b>; AZKT keeps only a hash of it{invite.invitation.expires_at ? <> · valid until <When iso={invite.invitation.expires_at} format="long" /></> : null}.</div>
              <div className="copybox">
                <code onClick={(e) => { const r = document.createRange(); r.selectNodeContents(e.currentTarget); window.getSelection()?.removeAllRanges(); window.getSelection()?.addRange(r); }}>{inviteUrl}</code>
                <Button size="sm" variant={copied ? "soft" : "primary"} onClick={copy}>{copied ? "Copied" : "Copy link"}</Button>
              </div>
              <div className="fs13 t3">Send it to {contactOf(invite.invitation)}. Their device creates a passkey when they open it; no password.</div>
            </>
          ) : (
            <Notice tone="risk" lead="Already invited">{invite.note || "An invitation is pending for this person. Revoke it from the list to issue a new link."}</Notice>
          )}
        </div>
      ) : (
        <div className="stack">
          <div className="form-grid">
            <Field label="Name" required><Input ref={nameRef} value={name} onChange={(e) => setName(e.target.value)} placeholder="Full name" autoComplete="off" /></Field>
            {isEdit ? (
              <>
                <Field label="Email"><Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@…" /></Field>
                <Field label="Phone"><Input type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+1 …" /></Field>
              </>
            ) : (
              <Field label="Phone or email" required hint="Where the invitation goes."><Input value={contact} onChange={(e) => setContact(e.target.value)} placeholder="+1 … or name@…" autoComplete="off" /></Field>
            )}
          </div>

          <div className="stack-sm">
            <div className="eyebrow">What they do</div>
            <div className="opts" role="radiogroup" aria-label="Role">
              {ROLE_OPTIONS.map((r) => (
                <button key={r} type="button" role="radio" aria-checked={role === r} className="opt" disabled={accessLocked} title={accessLocked ? accessReason : undefined} onClick={() => changeRole(r)}>
                  <span className="opt__mark" aria-hidden="true" />
                  <span><span className="opt__label">{ROLE_LABELS[r]}</span><span className="opt__desc"> · {ROLE_DESCRIPTIONS[r]}</span></span>
                </button>
              ))}
            </div>
          </div>

          <div className="row-wrap" style={{ gap: 16, alignItems: "flex-start" }}>
            <div className="stack-sm" style={{ flex: "1 1 240px" }}>
              <div className="eyebrow">Which vehicles</div>
              <SegmentedControl label="Vehicle scope" size="sm" value={scope} onChange={setScope} options={[
                { value: "all", label: "All vehicles", disabled: accessLocked, disabledReason: accessReason },
                { value: "assigned", label: "Only assigned to them", disabled: accessLocked || role === "owner", disabledReason: accessLocked ? accessReason : "Owners see everything." },
              ]} />
            </div>
            {role !== "owner" ? (
              <div className="stack-sm" style={{ flex: "1 1 240px" }}>
                <div className="eyebrow">Who they report to</div>
                <Select value={managerId} onChange={(e) => setManagerId(e.target.value)} aria-label="Reports to" disabled={accessLocked} title={accessLocked ? accessReason : undefined}>
                  <option value="">Nobody yet</option>
                  {managers.map((m) => <option key={m.id} value={m.id}>{m.id === user?.id ? "You" : m.display_name} · {ROLE_LABELS[m.role as Role] || m.role}</option>)}
                </Select>
              </div>
            ) : null}
          </div>

          <div className="stack-sm">
            <div className="between">
              <div className="eyebrow">What they can do <span style={{ textTransform: "none", letterSpacing: 0 }}>· {onCount} on{customCount ? ` · ${customCount} custom` : ""}</span></div>
              <button type="button" className="linklike fs13" disabled={!customCount || accessLocked} title={accessLocked ? accessReason : !customCount ? "Nothing is customised." : undefined} onClick={() => setOverrides({})}>Reset to {ROLE_LABELS[role]} defaults</button>
            </div>
            {role === "owner" ? <div className="fs13 t3">Owners can do everything, including Money.</div> : (
              <div className="perm-groups">
                {PERM_GROUPS.map((g) => (
                  <div key={g.label}>
                    <div className="perm-group__title">{g.label}</div>
                    {g.keys.map((k) => {
                      const locked = OWNER_LOCKED.has(k);
                      const moneyGrant = isEdit && k === "costs.read";
                      const custom = k in overrides && overrides[k] !== defaults[k];
                      const disabled = locked || accessLocked || moneyGrant;
                      const why = accessLocked ? accessReason : locked ? "Owner only" : moneyGrant ? "Money access is an explicit grant — use Grant / Revoke below." : undefined;
                      return <Switch key={k} checked={effective[k]} onChange={(on) => toggle(k, on)} label={PERM_LABELS[k]} meta={locked ? "Owner only" : custom ? "custom" : undefined} disabled={disabled} disabledReason={why} />;
                    })}
                  </div>
                ))}
              </div>
            )}
          </div>

          {isEdit && person && sv ? (
            <>
              <div className="stack-sm">
                <div className="eyebrow">Money</div>
                <div className="set-row" style={{ padding: "8px 10px", border: "1px solid var(--line2)", borderRadius: 12 }}>
                  <div className="set-row__main">
                    <span className="set-row__title">{person.perms?.["costs.read"] ? "Can see costs and margins" : "Cannot see costs or margins"}</span>
                    <span className="set-row__meta">{person.perms?.["costs.read"] ? "An explicit grant, recorded with who and when." : "Explicit owner grant required for cost detail."}</span>
                  </div>
                  <div className="set-row__right">
                    {person.role === "owner" ? <span className="fs13 t3">Owner</span> : person.perms?.["costs.read"] ? (
                      <Button size="sm" variant="soft" loading={busy("revoke")} disabled={accessLocked} disabledReason={accessReason} onClick={() => grant(false)}>Revoke</Button>
                    ) : (
                      <Button size="sm" variant="soft" loading={busy("grant")} disabled={accessLocked || person.status !== "active"} disabledReason={accessLocked ? accessReason : "Only active people can be granted access."} onClick={() => grant(true)}>Grant costs.read</Button>
                    )}
                  </div>
                </div>
                {person.grants?.length ? (
                  <Expander title="Grant history">
                    <KeyValues items={person.grants.slice(-6).reverse().map((g) => [<When key={g.at} iso={g.at} format="datetime" />, `${g.granted ? "Granted" : "Revoked"} ${g.perm}${g.note ? ` · ${g.note}` : ""}`])} />
                  </Expander>
                ) : null}
              </div>

              <Field label={confirmDisable ? "Why? (recorded)" : "Reason for this change (optional, recorded in Activity)"}>
                <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder={confirmDisable ? "e.g. left the shop" : "e.g. now runs the shop floor"} />
              </Field>

              <div className="set-row" style={{ padding: "8px 10px", border: "1px solid var(--line2)", borderRadius: 12 }}>
                <div className="set-row__main">
                  <span className="set-row__title">{sv.health ? <HealthLabel health={sv.health} label={sv.label} dot /> : sv.label}</span>
                  <span className="set-row__meta">
                    {person.status === "disabled" ? <>Disabled <When iso={person.disabled_at} relative />{person.disabled_reason ? ` · ${person.disabled_reason}` : ""}</> : person.last_seen_at ? <>Last seen <When iso={person.last_seen_at} relative /></> : "Hasn't signed in yet"}
                    {person.access_changed_at ? <> · access changed <When iso={person.access_changed_at} relative /></> : null}
                  </span>
                </div>
                <div className="set-row__right">
                  {person.status === "disabled" ? (
                    <Button size="sm" variant="soft" loading={busy("enable")} onClick={enable}>Restore access</Button>
                  ) : confirmDisable ? null : (
                    <Button size="sm" variant="soft" disabled={self} disabledReason="You can't disable yourself." onClick={() => setConfirmDisable(true)}>Disable access</Button>
                  )}
                </div>
              </div>
            </>
          ) : null}
        </div>
      )}
    </ResponsiveDialog>
  );
}
