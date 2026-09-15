/* Settings › Connections. GET /api/connections (freshness for every provider; identities/config for the owner).
   Google providers: POST /api/connections/google/{provider}/start → redirect. Token providers: POST /{provider}/secret
   (never echoed). Non-secret config: POST /{provider}/config. Disconnect with confirm. ?google=… toasts from the callback. */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, api, describeError } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useQuery } from "../../../lib/useQuery";
import { useIsMobile } from "../../../lib/viewport";
import { can, whyNot } from "../../../lib/perms";
import { Button, EmptyState, ErrorState, Field, GlassPanel, HealthLabel, Input, Loading, Notice, ResponsiveDialog, When, useToast } from "../../../ui";
import { freshnessHealth, type Connection, type ConnectionsResp } from "./types";

const GOOGLE = new Set(["gmail_business", "gmail_personal", "drive", "sheets", "google_calendar"]);
interface FieldSpec { key: string; label: string; hint?: string; secret?: boolean; list?: boolean; type?: string; placeholder?: string }
const SECRET_FIELDS: Record<string, FieldSpec[]> = {
  square: [{ key: "access_token", label: "Access token", secret: true, hint: "Square Developer › Credentials. Stored encrypted; never shown again." }],
  telegram: [{ key: "bot_token", label: "Bot token", secret: true, hint: "From @BotFather. Only paired people can talk to it." }],
  wordpress: [{ key: "username", label: "Username" }, { key: "app_password", label: "Application password", secret: true, hint: "WordPress › Users › Application passwords." }],
  woocommerce: [{ key: "consumer_key", label: "Consumer key", secret: true }, { key: "consumer_secret", label: "Consumer secret", secret: true }],
  smtp: [{ key: "username", label: "SMTP username" }, { key: "password", label: "SMTP password", secret: true }],
};
const CONFIG_FIELDS: Record<string, FieldSpec[]> = {
  drive: [{ key: "folder_id", label: "Importer folder ID", hint: "The long id in the folder's Drive URL." }],
  sheets: [{ key: "sheet_id", label: "Ledger sheet ID", hint: "The id in the spreadsheet's URL." }],
  gmail_personal: [
    { key: "allowlist_senders", label: "Allowed senders", hint: "Comma-separated addresses. Only these are read.", list: true, placeholder: "exporter@…, broker@…" },
    { key: "allowlist_domains", label: "Allowed domains", hint: "Comma-separated, e.g. exporter.co.jp", list: true },
  ],
  wordpress: [{ key: "site_url", label: "Site URL", placeholder: "https://…", type: "url" }],
  woocommerce: [{ key: "site_url", label: "Site URL", placeholder: "https://…", type: "url" }],
  smtp: [{ key: "host", label: "SMTP host" }, { key: "port", label: "Port", placeholder: "587" }, { key: "from_address", label: "From address", type: "email" }],
  square: [{ key: "location_id", label: "Location ID", hint: "Optional; limits payments to one location." }],
};
const SERVER_ONLY: Record<string, string> = {
  model: "The AI model key is set on the server (ANTHROPIC_API_KEY).",
  // `sms` (dropped) and the old `calendar` placeholder (replaced by the connectable google_calendar)
  // are no longer listed by the API; these stay only for a row written before those decisions.
  sms: "Business SMS was dropped and is not planned.",
  calendar: "Replaced by the business calendar row.",
  legacy_notion: "Legacy Notion mirror is configured on the server.",
  legacy_openclaw: "Legacy agents are configured on the server.",
};
const USED_FOR: Record<string, string> = {
  gmail_business: "Inbox, customer replies, promises", gmail_personal: "Exporter and broker mail only", drive: "Vehicle photos and documents",
  sheets: "Ledger import and cost matching", square: "Deposits and payments", telegram: "Reminders and Manager chat",
  wordpress: "Website listings", woocommerce: "Website listings", model: "Drafts, summaries, matching", smtp: "Reminder emails",
  google_calendar: "Calls and meetings on the calendar",
  sms: "Dropped", calendar: "Replaced", legacy_notion: "Read-only mirror", legacy_openclaw: "Read-only",
};

type DialogKind = "google" | "secret" | "config" | "disconnect";

export function ConnectionsSection() {
  const { user } = useAuth();
  const manage = can(user, "connections");
  const isMobile = useIsMobile();
  const { toast } = useToast();
  const [params, setParams] = useSearchParams();
  const q = useQuery<ConnectionsResp>((signal) => api.get<ConnectionsResp>("/api/connections", { signal }), []);
  const [dialog, setDialog] = useState<{ kind: DialogKind; c: Connection } | null>(null);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState<Record<string, string>>({});
  const [flags, setFlags] = useState<{ enable_send: boolean; enable_modify: boolean }>({ enable_send: false, enable_modify: false });
  const firstRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const g = params.get("google");
    if (!g) return;
    const provider = params.get("provider");
    const message = params.get("message");
    if (g === "connected") toast({ message: `Google connected${provider ? ` · ${provider.replace(/_/g, " ")}` : ""}.`, tone: "ok" });
    else if (g === "denied") toast({ message: "Google access was denied or cancelled. Nothing changed.", tone: "risk", duration: 6000 });
    else toast({ title: "Google connection failed", message: message || "The token exchange failed.", tone: "blocked", duration: 8000 });
    const next = new URLSearchParams(params);
    ["google", "provider", "message"].forEach((k) => next.delete(k));
    setParams(next, { replace: true });
    q.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const open = (kind: DialogKind, c: Connection) => {
    const cfg = (c.config || {}) as Record<string, unknown>;
    const init: Record<string, string> = {};
    for (const f of [...(CONFIG_FIELDS[c.provider] || []), ...(SECRET_FIELDS[c.provider] || [])]) {
      const v = cfg[f.key];
      init[f.key] = f.secret ? "" : Array.isArray(v) ? v.join(", ") : v === undefined || v === null ? "" : String(v);
    }
    if (c.provider === "gmail_personal") init.expected_identity = typeof cfg.expected_identity === "string" ? cfg.expected_identity : "";
    setForm(init);
    setFlags({ enable_send: (c.granted_scopes || []).some((s) => s.includes("gmail.send")), enable_modify: (c.granted_scopes || []).some((s) => s.includes("gmail.modify")) });
    setDialog({ kind, c });
  };

  const startGoogle = async (c: Connection) => {
    setSaving(true);
    try {
      const body: Record<string, unknown> = {};
      if (c.provider === "gmail_business") { body.enable_send = flags.enable_send; body.enable_modify = flags.enable_modify; }
      if (c.provider === "gmail_personal" && form.expected_identity?.trim()) body.expected_identity = form.expected_identity.trim();
      const r = await api.post<{ url: string; scopes: string[]; redirect_uri: string }>(`/api/connections/google/${encodeURIComponent(c.provider)}/start`, body);
      window.location.assign(r.url);
    } catch (e) {
      toast({ title: "Couldn't start Google sign-in", message: describeError(e), tone: "blocked", duration: 8000 });
      setSaving(false);
    }
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!dialog) return;
    const { kind, c } = dialog;
    if (kind === "google") { await startGoogle(c); return; }
    setSaving(true);
    try {
      if (kind === "secret") {
        const secret: Record<string, string> = {};
        for (const f of SECRET_FIELDS[c.provider] || []) if (form[f.key]?.trim()) secret[f.key] = form[f.key].trim();
        if (!Object.keys(secret).length) { toast({ message: "Enter the credentials first.", tone: "risk" }); setSaving(false); return; }
        await api.post(`/api/connections/${encodeURIComponent(c.provider)}/secret`, secret);
        const cfg = configBody(c.provider);
        if (Object.keys(cfg).length) await api.post(`/api/connections/${encodeURIComponent(c.provider)}/config`, cfg);
        toast({ message: `${c.label}: credentials stored. They are encrypted and never shown again.`, tone: "ok" });
      } else if (kind === "config") {
        await api.post(`/api/connections/${encodeURIComponent(c.provider)}/config`, configBody(c.provider));
        toast({ message: `${c.label}: settings saved.`, tone: "ok" });
      } else if (kind === "disconnect") {
        await api.post(`/api/connections/${encodeURIComponent(c.provider)}/disconnect`);
        toast({ message: `${c.label} disconnected. Dependent workflows pause until it is reconnected.`, tone: "ok" });
      }
      setDialog(null);
      q.reload();
    } catch (err) {
      toast({ message: describeError(err), tone: err instanceof ApiError && err.isNotFound ? "risk" : "blocked", duration: 6000 });
    } finally {
      setSaving(false);
    }
  };
  const configBody = (provider: string): Record<string, unknown> => {
    const out: Record<string, unknown> = {};
    for (const f of CONFIG_FIELDS[provider] || []) {
      const v = (form[f.key] || "").trim();
      if (f.list) out[f.key] = v ? v.split(",").map((s) => s.trim()).filter(Boolean) : [];
      else if (v) out[f.key] = f.key === "port" ? Number(v) || v : v;
    }
    return out;
  };

  const items = q.data?.items || [];
  const oauthOff = q.data ? !q.data.google_oauth_configured : false;
  const connected = (c: Connection) => c.status !== "disconnected" && c.freshness.state !== "disconnected";

  const actionsFor = (c: Connection) => {
    if (!manage) return null;
    const p = c.provider;
    if (SERVER_ONLY[p]) return <Button size="sm" variant="soft" disabled disabledReason={SERVER_ONLY[p]}>Configure</Button>;
    const isConn = connected(c);
    if (GOOGLE.has(p)) {
      return (
        <>
          {CONFIG_FIELDS[p] ? <Button size="sm" variant="soft" onClick={() => open("config", c)}>Settings</Button> : null}
          <Button size="sm" variant={isConn && c.freshness.state === "ok" ? "soft" : "primary"} disabled={oauthOff} disabledReason="Google OAuth client not configured — set GOOGLE_CLIENT_ID/SECRET on the server." onClick={() => open("google", c)}>{isConn ? "Reconnect" : "Connect Google"}</Button>
          {isConn ? <Button size="sm" variant="ghost" onClick={() => open("disconnect", c)}>Disconnect</Button> : null}
        </>
      );
    }
    if (SECRET_FIELDS[p]) {
      return (
        <>
          {CONFIG_FIELDS[p] && isConn ? <Button size="sm" variant="soft" onClick={() => open("config", c)}>Settings</Button> : null}
          <Button size="sm" variant={isConn ? "soft" : "primary"} onClick={() => open("secret", c)}>{isConn ? "Replace credentials" : "Enter credentials"}</Button>
          {isConn ? <Button size="sm" variant="ghost" onClick={() => open("disconnect", c)}>Disconnect</Button> : null}
        </>
      );
    }
    return <Button size="sm" variant="soft" disabled disabledReason="No setup for this provider yet.">Configure</Button>;
  };

  const d = dialog;
  const dialogTitle = d ? ({ google: `Connect ${d.c.label}`, secret: `${d.c.label} credentials`, config: `${d.c.label} settings`, disconnect: `Disconnect ${d.c.label}?` })[d.kind] : "";

  return (
    <div className="stack">
      {oauthOff ? <Notice tone="risk" lead="Google OAuth client not configured">Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET on the server, then connect Gmail, Drive and Sheets here.</Notice> : null}
      {q.data && !q.data.all_clear_possible ? <Notice tone="risk" lead="Home can't claim all-clear">{q.data.stale.join(", ")} {q.data.stale.length === 1 ? "is" : "are"} stale, so summaries are based on old data.</Notice> : null}
      <GlassPanel clip>
        {q.loading ? <Loading label="Loading connections" rows={4} /> : q.error ? <ErrorState error={q.error} onRetry={q.reload} /> : !items.length ? (
          <EmptyState title="No providers reported" body="The server lists providers once its connection registry is up." />
        ) : items.map((c) => {
          const fail = c.failure && "message" in c.failure && c.failure.message ? c.failure : null;
          return (
            <div key={c.provider} className="set-row">
              <div className="set-row__main">
                <span className="set-row__title">
                  <span>{c.label}</span>
                  <HealthLabel health={freshnessHealth(c.freshness.state)} label={c.freshness.label} dot />
                </span>
                <span className="set-row__meta">
                  {USED_FOR[c.provider] ? <span>{USED_FOR[c.provider]}</span> : null}
                  {manage && c.account_identity ? <> · {c.account_identity}</> : null}
                  {manage && c.coverage?.to ? <> · covered to <When iso={c.coverage.to} format="datetime" /></> : c.freshness.last_success_at ? <> · last sync <When iso={c.freshness.last_success_at} relative /></> : null}
                  {manage && c.watch_expires_at ? <> · watch until <When iso={c.watch_expires_at} format="datetime" /></> : null}
                  {manage && c.environment && q.data?.environment && c.environment !== q.data.environment ? <> · connected in {c.environment}</> : null}
                </span>
                {fail ? <span className="fs12" style={{ color: "var(--blocked)" }}>{fail.kind ? `${fail.kind.replace(/_/g, " ")}: ` : ""}{fail.message}{fail.at ? <> · <When iso={fail.at} relative /></> : null}</span> : null}
                {manage && c.provider === "gmail_business" && (c.granted_scopes || []).length ? <span className="fs12 t4">{(c.granted_scopes || []).some((s) => s.includes("gmail.send")) ? "Can send (with approval)" : "Read-only"}{(c.granted_scopes || []).some((s) => s.includes("gmail.modify")) ? " · labels and archive" : ""}</span> : null}
              </div>
              <div className="set-row__right">{actionsFor(c)}</div>
            </div>
          );
        })}
      </GlassPanel>
      <div className="set-foot">{manage ? "Secrets are stored encrypted and never shown again. A stale source makes Home say so instead of claiming all-clear." : `Read-only for your role. ${whyNot("connections")}`}</div>

      <ResponsiveDialog mobile={isMobile} open={!!d} onClose={() => setDialog(null)} title={dialogTitle} size="md" initialFocusRef={firstRef as React.RefObject<HTMLElement>}
        footer={d ? (
          <>
            <Button type="submit" form="conn-form" variant={d.kind === "disconnect" ? "danger" : "primary"} loading={saving}>{d.kind === "google" ? "Continue to Google" : d.kind === "disconnect" ? "Disconnect" : "Save"}</Button>
            <Button variant="ghost" onClick={() => setDialog(null)}>Cancel</Button>
            {d.kind === "secret" ? <span className="fs13 t3">Stored encrypted; never echoed back.</span> : null}
          </>
        ) : undefined}>
        {d ? (
          <form id="conn-form" className="stack" onSubmit={submit}>
            {d.kind === "google" ? (
              <>
                <div className="fs14 t2">You'll sign in with Google and come back here. AZKT asks for read access{d.c.provider === "gmail_business" ? " plus the options below" : ""}; tokens are stored encrypted.</div>
                {d.c.provider === "gmail_business" ? (
                  <div className="stack-sm">
                    <label className="row" style={{ gap: 10, minHeight: 44 }}><input type="checkbox" checked={flags.enable_send} onChange={(e) => setFlags((f) => ({ ...f, enable_send: e.target.checked }))} /> <span>Allow sending <span className="t3 fs13">· every send still needs your approval</span></span></label>
                    <label className="row" style={{ gap: 10, minHeight: 44 }}><input type="checkbox" checked={flags.enable_modify} onChange={(e) => setFlags((f) => ({ ...f, enable_modify: e.target.checked }))} /> <span>Allow labels and archive <span className="t3 fs13">· separate capability, reversible</span></span></label>
                  </div>
                ) : null}
                {d.c.provider === "gmail_personal" ? (
                  <Field label="Expected account" hint="AZKT refuses the connection if Google returns a different address."><Input ref={firstRef} type="email" value={form.expected_identity || ""} onChange={(e) => setForm((f) => ({ ...f, expected_identity: e.target.value }))} placeholder="you@gmail.com" /></Field>
                ) : null}
              </>
            ) : d.kind === "disconnect" ? (
              <div className="fs14 t2">{d.c.account_identity ? `${d.c.account_identity} is disconnected and its token revoked.` : "The stored credential is removed."} Workflows that depend on it{d.c.dependent_workflows?.length ? ` (${d.c.dependent_workflows.join(", ")})` : ""} pause until you reconnect. Nothing already recorded is deleted.</div>
            ) : (
              <>
                {(d.kind === "secret" ? [...(SECRET_FIELDS[d.c.provider] || []), ...(CONFIG_FIELDS[d.c.provider] || [])] : CONFIG_FIELDS[d.c.provider] || []).map((f, i) => (
                  <Field key={f.key} label={f.label} hint={f.hint}>
                    <Input ref={i === 0 ? firstRef : undefined} type={f.secret ? "password" : f.type || "text"} autoComplete={f.secret ? "new-password" : "off"} value={form[f.key] || ""} onChange={(e) => setForm((v) => ({ ...v, [f.key]: e.target.value }))} placeholder={f.secret ? "••••••••" : f.placeholder} />
                  </Field>
                ))}
              </>
            )}
          </form>
        ) : null}
      </ResponsiveDialog>
    </div>
  );
}
