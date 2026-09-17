/* Ways to sign in. Every passkey on this account (GET /auth/credentials), rename
   (POST /auth/credentials/{id}/rename), revoke (DELETE), and the two ways to add one:
   this device (useAuth().addPasskey) or another device, with a one-time link and QR
   (POST/GET/DELETE /auth/device-link). Shown to every signed-in person. */
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, describeError } from "../../../lib/api";
import { useAuth, type DeviceEnrollment, type DeviceLink } from "../../../lib/auth";
import { useQuery } from "../../../lib/useQuery";
import { Button, Dialog, EmptyState, ErrorState, GlassPanel, Input, Loading, Notice, QrCode, When, useToast } from "../../../ui";

interface Credential {
  id: string;
  label: string | null;
  transports: string[] | null;
  sign_count: number;
  created_at: string | null;
  last_used_at: string | null;
}

/** "internal" is this device's own Face ID / fingerprint; "hybrid" is a phone scanned from another device. */
const TRANSPORT_LABELS: Record<string, string> = {
  internal: "this device", hybrid: "phone or tablet", usb: "security key",
  nfc: "security key (NFC)", ble: "security key (Bluetooth)", "smart-card": "smart card", cable: "cable",
};

function transportSummary(transports: string[] | null): string {
  const named = (transports || []).map((t) => TRANSPORT_LABELS[t]).filter(Boolean);
  return named.length ? named[0] : "";
}

function countdown(iso: string | null, now: number): string | null {
  if (!iso) return null;
  const left = Math.round((new Date(iso).getTime() - now) / 1000);
  if (left <= 0) return null;
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

export function PasskeysBlock() {
  const { addPasskey, busy, logout, user } = useAuth();
  const { toast } = useToast();
  const nav = useNavigate();
  const q = useQuery<Credential[]>((signal) => api.get<Credential[]>("/auth/credentials", { signal }), []);
  const live = useQuery<{ enrollment: DeviceEnrollment | null }>((signal) => api.get("/auth/device-link", { signal }), []);
  const [label, setLabel] = useState("");
  const [renaming, setRenaming] = useState<{ id: string; label: string } | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const [link, setLink] = useState<DeviceLink | null>(null);
  const [minting, setMinting] = useState(false);
  const [copied, setCopied] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const knownCount = useRef(0);
  const list = q.data || [];
  const expiresIn = link ? countdown(link.enrollment.expires_at, now) : null;

  // While the link is up, the desktop watches for the passkey the other device is making.
  useEffect(() => {
    if (!link) return;
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    const poll = window.setInterval(async () => {
      try {
        const creds = await api.get<Credential[]>("/auth/credentials");
        if (creds.length > knownCount.current) {
          const added = creds[creds.length - 1];
          setLink(null);
          q.reload();
          live.reload();
          toast({ message: `"${added.label || "New device"}" added. It can sign in on its own now.`, tone: "ok", duration: 6000 });
        }
      } catch { /* a blip: the next tick tries again */ }
    }, 3000);
    return () => { window.clearInterval(tick); window.clearInterval(poll); };
  }, [link]);  // eslint-disable-line react-hooks/exhaustive-deps

  const addHere = async () => {
    try { await addPasskey(label.trim() || undefined); toast({ message: "Passkey added to this device.", tone: "ok" }); setLabel(""); q.reload(); }
    catch (e) { toast({ message: e instanceof Error ? e.message : "Couldn't add a passkey.", tone: "risk", duration: 6000 }); }
  };

  const mint = async () => {
    setMinting(true);
    setCopied(false);
    try {
      knownCount.current = list.length;
      const r = await api.post<DeviceLink>("/auth/device-link", { label: label.trim() || undefined });
      setNow(Date.now());
      setLink(r);
      setLabel("");
      live.reload();
    } catch (e) { toast({ message: describeError(e), tone: "blocked", duration: 6000 }); }
    finally { setMinting(false); }
  };

  const cancelLink = useCallback(async () => {
    try {
      await api.del("/auth/device-link");
      setLink(null);
      live.reload();
      toast({ message: "Link cancelled. It no longer adds anything.", tone: "ok" });
    } catch (e) { toast({ message: describeError(e), tone: "blocked" }); }
  }, [live, toast]);

  const copy = async () => {
    if (!link) return;
    try { await navigator.clipboard.writeText(link.url); setCopied(true); }
    catch { toast({ message: "Couldn't copy — select the link and copy it by hand.", tone: "risk" }); }
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

  const pending = live.data?.enrollment;

  return (
    <div className="stack">
      <div className="eyebrow">Ways to sign in <span style={{ textTransform: "none", letterSpacing: 0 }}>· {user?.display_name}</span></div>
      {pending && !link ? (
        <Notice tone="wait" lead="An add-a-device link is out there"
               action={<Button size="sm" variant="ghost" onClick={() => cancelLink()}>Cancel it</Button>}>
          Made <When iso={pending.created_at} relative />, valid until <When iso={pending.expires_at} format="time" />. Whoever opens it adds a way into this account, so cancel it if you didn't finish.
        </Notice>
      ) : null}
      <GlassPanel clip>
        {q.loading ? <Loading label="Loading passkeys" rows={2} /> : q.error ? <ErrorState error={q.error} onRetry={q.reload} /> : !list.length ? (
          <EmptyState align="left" title="No passkeys listed" body="You are signed in, so at least one exists; the list will show once the server answers." />
        ) : list.map((c) => (
          <div key={c.id} className="set-row">
            <div className="set-row__main">
              {renaming?.id === c.id ? (
                <span className="row"><Input value={renaming.label} onChange={(e) => setRenaming({ id: c.id, label: e.target.value })} aria-label="Passkey name" maxLength={80} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void rename(); } if (e.key === "Escape") setRenaming(null); }} autoFocus /></span>
              ) : <span className="set-row__title">{c.label || "passkey"}</span>}
              <span className="set-row__meta">
                Added <When iso={c.created_at} format="date" />
                {transportSummary(c.transports) ? ` · ${transportSummary(c.transports)}` : ""}
                {c.last_used_at ? <> · last used <When iso={c.last_used_at} relative /></> : " · not used yet"}
              </span>
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
        <Button variant="primary" loading={minting} onClick={mint}>Add another device</Button>
        <Button variant="soft" loading={busy} onClick={addHere}>Add to this device</Button>
        <Button variant="ghost" onClick={() => { void logout().then(() => nav("/login", { replace: true })); }}>Sign out</Button>
      </div>
      <div className="set-foot">A passkey never leaves the device that made it, so each device you sign in from needs its own. <b style={{ fontWeight: 600 }}>Add another device</b> shows a code to scan with your phone; <b style={{ fontWeight: 600 }}>Add to this device</b> makes one right here. No passwords, no recovery codes: if every passkey is lost, the owner re-invites you (or, for the owner, re-runs setup with the server token).</div>

      <Dialog
        open={!!link}
        onClose={() => setLink(null)}
        title="Add another device"
        size="lg"
        align="top"
        description="Scan this with the phone or tablet you want to sign in from. It makes its own passkey and is signed in straight away."
        footer={<><Button variant="ghost" onClick={() => { void cancelLink(); }}>Cancel link</Button><Button variant="primary" onClick={() => setLink(null)}>Done</Button></>}
      >
        {link ? (
          <div className="stack">
            {link.qr ? (
              <div className="qr-wrap"><QrCode rows={link.qr.rows} label="Open this link on the device you are adding" /></div>
            ) : null}
            <div className="copybox">
              <code onClick={(e) => { const r = document.createRange(); r.selectNodeContents(e.currentTarget); window.getSelection()?.removeAllRanges(); window.getSelection()?.addRange(r); }}>{link.url}</code>
              <Button size="sm" variant={copied ? "soft" : "primary"} onClick={copy}>{copied ? "Copied" : "Copy link"}</Button>
            </div>
            {expiresIn ? (
              <Notice tone="wait" lead={`Expires in ${expiresIn}`}>Works once, on one device. AZKT keeps only a hash of it, so it is shown here and nowhere else.</Notice>
            ) : (
              <Notice tone="risk" lead="This link has expired" action={<Button size="sm" variant="primary" loading={minting} onClick={mint}>New link</Button>}>Make a new one and scan it within {link.expires_in_minutes} minutes.</Notice>
            )}
            {expiresIn ? <div className="fs13 t3">Waiting for the other device… this closes on its own once the passkey is made.</div> : null}
          </div>
        ) : null}
      </Dialog>
    </div>
  );
}
