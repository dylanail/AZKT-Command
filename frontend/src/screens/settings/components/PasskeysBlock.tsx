/* Passkeys on this account: GET /auth/credentials, rename (POST /auth/credentials/{id}/rename), revoke (DELETE),
   add via useAuth().addPasskey, and Sign out. Shown to every signed-in person. */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, describeError } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useQuery } from "../../../lib/useQuery";
import { Button, EmptyState, ErrorState, GlassPanel, Input, Loading, When, useToast } from "../../../ui";

interface Credential { id: string; label: string | null; transports: string[] | null; sign_count: number; created_at: string | null }

export function PasskeysBlock() {
  const { addPasskey, busy, logout, user } = useAuth();
  const { toast } = useToast();
  const nav = useNavigate();
  const q = useQuery<Credential[]>((signal) => api.get<Credential[]>("/auth/credentials", { signal }), []);
  const [label, setLabel] = useState("");
  const [renaming, setRenaming] = useState<{ id: string; label: string } | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const list = q.data || [];

  const add = async () => {
    try { await addPasskey(label.trim() || undefined); toast({ message: "Passkey added to this device.", tone: "ok" }); setLabel(""); q.reload(); }
    catch (e) { toast({ message: e instanceof Error ? e.message : "Couldn't add a passkey.", tone: "risk", duration: 6000 }); }
  };
  const rename = async () => {
    if (!renaming || !renaming.label.trim()) return;
    setWorking(renaming.id);
    try { await api.post(`/auth/credentials/${encodeURIComponent(renaming.id)}/rename`, { label: renaming.label.trim() }); toast({ message: "Renamed.", tone: "ok" }); setRenaming(null); q.reload(); }
    catch (e) { toast({ message: describeError(e), tone: "blocked" }); }
    finally { setWorking(null); }
  };
  const revoke = async (c: Credential) => {
    setWorking(c.id);
    try { await api.del(`/auth/credentials/${encodeURIComponent(c.id)}`); toast({ message: `Passkey "${c.label || "passkey"}" revoked. That device can no longer unlock AZKT.`, tone: "ok" }); q.reload(); }
    catch (e) { toast({ message: describeError(e), tone: "blocked", duration: 6000 }); }
    finally { setWorking(null); }
  };

  return (
    <div className="stack">
      <div className="eyebrow">Passkeys on this account <span style={{ textTransform: "none", letterSpacing: 0 }}>· {user?.display_name}</span></div>
      <GlassPanel clip>
        {q.loading ? <Loading label="Loading passkeys" rows={2} /> : q.error ? <ErrorState error={q.error} onRetry={q.reload} /> : !list.length ? (
          <EmptyState align="left" title="No passkeys listed" body="You are signed in, so at least one exists; the list will show once the server answers." />
        ) : list.map((c) => (
          <div key={c.id} className="set-row">
            <div className="set-row__main">
              {renaming?.id === c.id ? (
                <span className="row"><Input value={renaming.label} onChange={(e) => setRenaming({ id: c.id, label: e.target.value })} aria-label="Passkey name" maxLength={80} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void rename(); } if (e.key === "Escape") setRenaming(null); }} autoFocus /></span>
              ) : <span className="set-row__title">{c.label || "passkey"}</span>}
              <span className="set-row__meta">Added <When iso={c.created_at} format="date" />{c.transports?.length ? ` · ${c.transports.join(", ")}` : ""} · used {c.sign_count} {c.sign_count === 1 ? "time" : "times"}</span>
            </div>
            <div className="set-row__right">
              {renaming?.id === c.id ? (
                <><Button size="sm" variant="primary" loading={working === c.id} disabled={!renaming.label.trim()} disabledReason="Give it a name." onClick={rename}>Save</Button><Button size="sm" variant="ghost" onClick={() => setRenaming(null)}>Cancel</Button></>
              ) : (
                <><Button size="sm" variant="soft" onClick={() => setRenaming({ id: c.id, label: c.label || "" })}>Rename</Button><Button size="sm" variant="ghost" loading={working === c.id} disabled={list.length <= 1} disabledReason="Add another passkey first — you can't revoke your only one." onClick={() => revoke(c)}>Revoke</Button></>
              )}
            </div>
          </div>
        ))}
      </GlassPanel>
      <div className="row-wrap">
        <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Device name (e.g. iPhone)" aria-label="New passkey name" style={{ maxWidth: 240 }} />
        <Button variant="primary" loading={busy} onClick={add}>Add a passkey to this device</Button>
        <Button variant="ghost" onClick={() => { void logout().then(() => nav("/login", { replace: true })); }}>Sign out</Button>
      </div>
      <div className="set-foot">Add one on each device you unlock from. No passwords, no recovery codes: if every passkey is lost, the owner re-invites you (or, for the owner, re-runs setup with the server token).</div>
    </div>
  );
}
