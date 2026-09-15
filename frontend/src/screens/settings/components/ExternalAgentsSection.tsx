/* Settings › External agents (spec §10.8). Owner only.
   Reads:  GET /api/settings/external-clients          registered clients, connect URLs, scope list, MCP tools
           GET /api/settings/external-clients/health    live state, in-flight missions, request totals
           GET /api/settings/external-clients/{id}/requests
           GET /api/settings/external-clients/{id}/callbacks   signed callback deliveries and every attempt
   Writes: POST /api/settings/external-clients          register — the key is shown exactly once
           POST /api/settings/external-clients/{id}/rotate | revoke | set-callback | test-callback
   A client's effective access is your rights ∩ its scopes ∩ its record scope; the server decides, always. */
import { useMemo, useState } from "react";
import { api } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useCommand } from "../../../lib/useCommand";
import { useQuery } from "../../../lib/useQuery";
import { Button, Chip, EmptyState, ErrorState, Expander, GlassPanel, Loading, Menu, Notice, When, useToast } from "../../../ui";
import {
  BASE, CALLBACK_STATE_LABELS, CallbackDialog, CallbacksDialog, RegisterDialog, RequestsDialog, RevokeDialog,
  TRANSPORT_LABELS, callbackStateTone, clientState, recordScopeSummary, scopeLabel,
  type ClientsResp, type ExternalClient, type HealthResp, type RegisterResult,
} from "./ExternalAgentsParts";

type DialogKind = "register" | "callback" | "revoke" | "requests" | "callbacks";

/* How this client hears that work finished. "Address saved, no signing secret" is not the same as "on":
   AZKT never pushes an unsigned body, so that client is still polling-only and must be told so. */
function callbackLine(c: ExternalClient) {
  if (!c.callback_configured) return { text: "polling only", tone: null as null | "ok" | "risk" | "blocked" | "wait", detail: "" };
  if (!c.callback_signing_configured) return { text: "callback needs a signing secret", tone: "risk" as const, detail: "" };
  const h = c.callbacks;
  if (!h || !h.last_state) return { text: "callback set", tone: "ok" as const, detail: "" };
  return {
    text: `callback ${CALLBACK_STATE_LABELS[h.last_state]?.toLowerCase() || h.last_state}`,
    tone: callbackStateTone(h.last_state),
    detail: h.failed ? ` · ${h.failed} needing attention` : "",
  };
}

function CopyLine({ label, value }: { label: string; value: string }) {
  const { toast } = useToast();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(value); setCopied(true); toast({ message: `${label} copied.`, tone: "ok" }); }
    catch { toast({ message: "Couldn't copy — select the text and copy it by hand.", tone: "risk" }); }
  };
  return (
    <div className="stack-sm">
      <div className="eyebrow">{label}</div>
      <div className="copybox">
        <code>{value}</code>
        <Button size="sm" variant={copied ? "soft" : "glass"} onClick={() => void copy()}>{copied ? "Copied" : "Copy"}</Button>
      </div>
    </div>
  );
}

export function ExternalAgentsSection() {
  const { user } = useAuth();
  const { toast } = useToast();
  const { run, busy } = useCommand();
  const owner = user?.role === "owner";

  const [tick, setTick] = useState(0);
  // Both endpoints are owner-only; someone else never fires them, they read the notice below instead.
  const clientsQ = useQuery<ClientsResp | null>(
    (signal) => (owner ? api.get<ClientsResp>(BASE, { signal }) : Promise.resolve(null)), [tick, owner]);
  const healthQ = useQuery<HealthResp | null>(
    (signal) => (owner ? api.get<HealthResp>(`${BASE}/health`, { signal }) : Promise.resolve(null)), [tick, owner]);

  const [dialog, setDialog] = useState<{ kind: DialogKind; client: ExternalClient | null } | null>(null);
  const [issued, setIssued] = useState<{ token: string; note: string; name: string; how: "registered" | "rotated" } | null>(null);
  const [copied, setCopied] = useState(false);

  const clients = useMemo(() => {
    const live = new Map((healthQ.data?.items || []).map((c) => [c.id, c]));
    return (clientsQ.data?.items || []).map((c) => ({ ...c, ...(live.get(c.id) || {}) }));
  }, [clientsQ.data, healthQ.data]);

  const reload = () => setTick((t) => t + 1);

  const copyToken = async () => {
    if (!issued) return;
    try { await navigator.clipboard.writeText(issued.token); setCopied(true); toast({ message: "Key copied.", tone: "ok" }); }
    catch { toast({ message: "Couldn't copy — select the key and copy it by hand.", tone: "risk" }); }
  };

  const rotate = async (c: ExternalClient) => {
    const r = await run<{ token?: string; token_note?: string }>(`rotate-${c.id}`, `${BASE}/${encodeURIComponent(c.id)}/rotate`,
      { expected_version: c.version, reason: "rotated from Settings" });
    if (r?.status === "ok" && r.data?.token) {
      setIssued({ token: r.data.token, note: r.data.token_note || "Store this now — the previous key no longer works.", name: c.name, how: "rotated" });
      setCopied(false);
      reload();
    }
  };

  if (!owner) {
    return (
      <Notice tone="neutral" lead="Owner only">
        External agents connect with a key that carries the owner's access, so only the owner can see or change them. Ask Dylan
        if something needs connecting.
      </Notice>
    );
  }

  return (
    <div className="stack">
      <div className="fs14 t2" style={{ maxWidth: 680 }}>
        Another agent — yours or someone else's — can talk to your Manager with its own key. It only ever gets what its scopes and
        your own permissions both allow, every request is recorded, and anything consequential comes back to you as an approval.
      </div>

      <div className="row-wrap">
        <Button variant="primary" size="md" onClick={() => setDialog({ kind: "register", client: null })}>Register a client</Button>
        <span className="fs13 t3">{clients.length} registered</span>
      </div>

      {issued ? (
        <GlassPanel padded className="stack-sm">
          <div className="eyebrow">{issued.how === "rotated" ? "New key" : "Key for"} {issued.name}</div>
          <div className="fs14">{issued.note}</div>
          <div className="copybox">
            <code
              onClick={(e) => {
                const r = document.createRange();
                r.selectNodeContents(e.currentTarget);
                window.getSelection()?.removeAllRanges();
                window.getSelection()?.addRange(r);
              }}
            >
              {issued.token}
            </code>
            <Button size="sm" variant={copied ? "soft" : "primary"} onClick={() => void copyToken()}>{copied ? "Copied" : "Copy key"}</Button>
          </div>
          <div className="row-wrap">
            <Button size="sm" variant="ghost" onClick={() => setIssued(null)}>I've saved it</Button>
            <span className="fs13 t3">AZKT keeps only a hash of it and can never show it again.</span>
          </div>
        </GlassPanel>
      ) : null}

      {clientsQ.loading ? (
        <GlassPanel clip><Loading label="Loading external agents" rows={2} /></GlassPanel>
      ) : clientsQ.error ? (
        <GlassPanel clip><ErrorState error={clientsQ.error} onRetry={reload} title="Couldn't load external agents" /></GlassPanel>
      ) : clients.length === 0 ? (
        <GlassPanel clip>
          <EmptyState
            title="No external agents yet"
            body="Register one when you want another assistant to ask your Manager about your trucks, or to file tasks and notes for you."
          />
        </GlassPanel>
      ) : (
        <GlassPanel clip>
          {clients.map((c) => {
            const state = clientState(c);
            const revoked = (c.state || c.status) === "revoked";
            return (
              <div key={c.id} className="set-row">
                <div className="set-row__main">
                  <div className="set-row__title">
                    <span className="truncate">{c.name}</span>
                    <Chip size="sm" tone={state.tone}>{state.label}</Chip>
                    <Chip size="sm" tone="soft">{TRANSPORT_LABELS[c.transport] || c.transport}</Chip>
                  </div>
                  <div className="row-wrap" style={{ gap: 6 }}>
                    {(c.scopes || []).map((s) => <Chip key={s} size="sm" tone="soft" title={s}>{scopeLabel(s)}</Chip>)}
                    {(c.scopes || []).length === 0 ? <span className="fs13 t4">No permissions granted</span> : null}
                  </div>
                  <div className="set-row__meta">
                    {recordScopeSummary(c)}
                    {" · key "}{c.token_prefix || "unknown"}…
                    {" · used "}{c.use_count}{" time"}{c.use_count === 1 ? "" : "s"}
                    {c.last_used_at ? <> · last <When iso={c.last_used_at} relative /></> : " · never used"}
                    {typeof c.in_flight_missions === "number" ? ` · ${c.in_flight_missions} in flight` : ""}
                    {typeof c.requests_total === "number" ? ` · ${c.requests_total} requests` : ""}
                  </div>
                  <div className="set-row__meta row-wrap" style={{ gap: 6 }}>
                    {(() => {
                      const cb = callbackLine(c);
                      return cb.tone
                        ? <Chip size="sm" tone={cb.tone}>{cb.text}{cb.detail}</Chip>
                        : <span className="fs13 t4">{cb.text}</span>;
                    })()}
                    {c.callbacks?.last_error ? <span className="fs12 t4 truncate" title={c.callbacks.last_error}>{c.callbacks.last_error}</span> : null}
                  </div>
                  <div className="set-row__meta">
                    {`Limits: ${c.quota?.per_minute ?? "—"}/min · ${c.quota?.concurrent ?? "—"} at once · ${c.quota?.per_day ?? "—"}/day`}
                    {c.expires_at ? <> · stops <When iso={c.expires_at} format="date" /></> : " · no end date"}
                    {c.notes ? ` · ${c.notes}` : ""}
                  </div>
                </div>
                <div className="set-row__right row-actions">
                  <Button
                    size="md"
                    variant="soft"
                    loading={busy(`rotate-${c.id}`)}
                    disabled={revoked}
                    disabledReason="This client is revoked. Register a new one instead."
                    onClick={() => void rotate(c)}
                  >
                    New key
                  </Button>
                  <Menu
                    label={`More actions for ${c.name}`}
                    align="right"
                    trigger={<Button size="md" variant="glass">More</Button>}
                    items={[
                      { label: "Recent requests", onSelect: () => setDialog({ kind: "requests", client: c }) },
                      { label: "Recent callbacks", onSelect: () => setDialog({ kind: "callbacks", client: c }) },
                      { label: c.callback_configured ? "Change callback" : "Set callback", onSelect: () => setDialog({ kind: "callback", client: c }) },
                      {
                        label: "Revoke access",
                        sepBefore: true,
                        disabled: revoked,
                        disabledReason: "Already revoked.",
                        onSelect: () => setDialog({ kind: "revoke", client: c }),
                      },
                    ]}
                  />
                </div>
              </div>
            );
          })}
        </GlassPanel>
      )}

      {healthQ.error ? (
        <Notice tone="wait" lead="Live figures unavailable">
          In-flight and request counts could not be read just now. The list above is still correct.
        </Notice>
      ) : null}

      {clientsQ.data ? (
        <GlassPanel padded className="stack">
          <h3>How to connect</h3>
          <div className="fs14 t2">
            Paste the key as <code>Authorization: Bearer …</code>. Give the client whichever address it understands.
          </div>
          <CopyLine label="HTTP base address" value={clientsQ.data.connect} />
          <CopyLine label="MCP address" value={clientsQ.data.mcp_url} />
          <div className="stack-sm">
            <div className="eyebrow">Tools a connected agent can use</div>
            <ul className="stack-sm" style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
              {(clientsQ.data.mcp_tools || []).map((t) => (
                <li key={t.name}><code>{t.name}</code> — {t.purpose}</li>
              ))}
            </ul>
          </div>
          <Expander title="Sources and technical details">
            <div className="fs13 stack-sm">
              <div>Permissions available to a client: {(clientsQ.data.scopes || []).join(", ") || "none listed"}.</div>
              <div>
                A dropped connection is not a second command: the client repeats its <code>request_key</code> and gets the same
                answer back. Over its limit it receives 429 with a wait time.
              </div>
              <div>
                Clients read progress with <code>GET /work/&#123;request_id&#125;?cursor=</code>. If you set a callback address for
                one, AZKT also posts that same result there once, signed, when the work finishes — only ever to the address you
                saved, never to one supplied in a request.
              </div>
            </div>
          </Expander>
        </GlassPanel>
      ) : null}

      <RegisterDialog
        open={dialog?.kind === "register"}
        scopes={clientsQ.data?.scopes || []}
        onClose={() => setDialog(null)}
        onRegistered={(r: RegisterResult) => {
          setIssued({ token: r.token, note: r.token_note || "Store this now — AZKT keeps only its hash.", name: r.client?.name || "the new client", how: "registered" });
          setCopied(false);
          reload();
        }}
      />
      <CallbackDialog open={dialog?.kind === "callback"} client={dialog?.client || null} onClose={() => setDialog(null)} onSaved={reload} />
      <CallbacksDialog open={dialog?.kind === "callbacks"} client={dialog?.client || null} onClose={() => { setDialog(null); reload(); }} />
      <RevokeDialog open={dialog?.kind === "revoke"} client={dialog?.client || null} onClose={() => setDialog(null)} onRevoked={reload} />
      <RequestsDialog open={dialog?.kind === "requests"} client={dialog?.client || null} onClose={() => setDialog(null)} />
    </div>
  );
}
