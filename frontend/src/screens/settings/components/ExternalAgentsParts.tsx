/* Settings › External agents — shared shapes, plain-language labels and the five dialogs.
   Contract: backend/app/routers/external_clients.py and backend/app/services/external_clients.py.
   Every write goes through useCommand, so "needs review" and "blocked" are explained by the server. */
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../../lib/api";
import { useCommand } from "../../../lib/useCommand";
import { useQuery } from "../../../lib/useQuery";
import { useIsMobile } from "../../../lib/viewport";
import { humanize } from "../../../lib/links";
import {
  Button, Chip, EmptyState, ErrorState, Expander, Field, Input, Loading, Notice, ResponsiveDialog, Select, Table,
  Textarea, Tr, When,
} from "../../../ui";

/* ---------- shapes ---------- */
export interface ExternalClient {
  id: string;
  name: string;
  transport: string;
  status: string;
  token_prefix: string;
  scopes: string[];
  record_scope: { vehicle_ids?: string[] };
  quota: { per_minute?: number; concurrent?: number; per_day?: number };
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
  use_count: number;
  callback_url: string | null;
  callback_configured: boolean;
  /** An address with no signing key cannot be signed, so nothing is ever pushed to it. */
  callback_signing_configured: boolean;
  callbacks_enabled: boolean;
  owner_user_id: string;
  notes: string;
  version: number;
  created_at: string | null;
  rotated_from_id: string | null;
  health?: Record<string, unknown>;
  /** From GET /health only. */
  in_flight_missions?: number;
  requests_total?: number;
  callbacks?: CallbackHealth;
  state?: string;
}

export interface CallbackHealth {
  total: number;
  pending: number;
  failed: number;
  last_state: string | null;
  last_event: string | null;
  last_at: string | null;
  last_error: string | null;
}

export interface CallbackAttempt {
  n: number;
  at: string | null;
  outcome: string;
  status: number | null;
  error: string | null;
  duration_ms: number | null;
  /** The destination host, not the full address: a webhook path can itself be a secret. */
  host: string | null;
}

export interface CallbackDeliveryRow {
  id: string;
  request_id: string | null;
  mission_id: string | null;
  event: string;
  request_state: string | null;
  state: string;
  attempts: number;
  max_attempts: number;
  response_status: number | null;
  error: string | null;
  cancel_reason: string | null;
  destination_host: string;
  signed: boolean;
  signature_version: string;
  next_attempt_at: string | null;
  sent_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  attempt_log: CallbackAttempt[];
  summary: string;
}

export interface CallbacksResp {
  items: CallbackDeliveryRow[];
  count: number;
  states: Record<string, number>;
  note: string;
  polling_only: boolean;
  client: ExternalClient;
  verification: {
    signature: { algorithm: string; version: string; signed_value: string; encoding: string; compare_with: string };
    headers: Record<string, string>;
    replay: { reject_if_older_than_seconds: number; idempotency_key: string; note: string };
    expected_response: string;
    when: string;
  };
}

export interface ClientsResp {
  items: ExternalClient[];
  count: number;
  scopes: string[];
  connect: string;
  mcp_url: string;
  mcp_tools: Array<{ name: string; purpose: string }>;
}
export interface HealthResp { items: ExternalClient[]; count: number }
export interface RegisterResult {
  client: ExternalClient;
  token: string;
  token_note: string;
  mcp_url?: string;
  http_base?: string;
}
export interface DelegatedRequestRow {
  id: string;
  request_key: string;
  kind: string;
  status: string;
  mission_id: string | null;
  message: string;
  cursor: number;
  depth: number;
  error: string | null;
  created_at: string | null;
}

export const BASE = "/api/settings/external-clients";
export const DEFAULT_QUOTA = { per_minute: 60, concurrent: 4, per_day: 2000 };

/* ---------- plain language ---------- */
export const SCOPE_LABELS: Record<string, string> = {
  "read:vehicles": "See vehicles",
  "read:tasks": "See tasks",
  "read:contacts": "See contacts",
  "read:sales": "See sales leads",
  "read:requests": "See import requests",
  "read:shipping": "See shipments",
  "read:costs": "See costs and margins",
  "read:photos": "See photos",
  "read:sources": "See where facts came from",
  "read:activity": "See the activity log",
  "write:tasks": "Create and update tasks",
  "write:vehicles": "Edit vehicle records",
  "write:contacts": "Edit contacts",
  "write:sales": "Update sales leads",
  "write:requests": "Update import requests",
  "write:notes": "Add notes",
  intake: "Send photos and voice notes for intake",
  "draft:messages": "Draft customer messages — never send them",
  ask: "Ask the Manager questions",
};
export const scopeLabel = (s: string) => SCOPE_LABELS[s] || humanize(s);

export const TRANSPORT_LABELS: Record<string, string> = {
  both: "MCP and HTTP",
  mcp: "MCP only",
  http: "HTTP only",
};

export function scopeGroup(s: string): "read" | "write" | "other" {
  if (s.startsWith("read:")) return "read";
  if (s.startsWith("write:")) return "write";
  return "other";
}

export const GROUP_TITLES: Record<"read" | "write" | "other", string> = {
  read: "What it may read",
  write: "What it may change",
  other: "What else it may do",
};

export function recordScopeSummary(c: ExternalClient): string {
  const ids = c.record_scope?.vehicle_ids || [];
  if (!ids.length) return "All of your records";
  return `${ids.length} vehicle${ids.length === 1 ? "" : "s"} only`;
}

export function clientState(c: ExternalClient): { tone: "ok" | "risk" | "blocked" | "wait"; label: string } {
  const state = c.state || c.status;
  if (state === "active") return { tone: "ok", label: "Active" };
  if (state === "revoked") return { tone: "blocked", label: "Revoked" };
  if (state === "expired") return { tone: "risk", label: "Expired" };
  if (state === "paused") return { tone: "wait", label: "Paused" };
  return { tone: "wait", label: humanize(state) };
}

/* ---------- register ---------- */
export interface RegisterDialogProps {
  open: boolean;
  scopes: string[];
  onClose: () => void;
  onRegistered: (r: RegisterResult) => void;
}

export function RegisterDialog({ open, scopes, onClose, onRegistered }: RegisterDialogProps) {
  const isMobile = useIsMobile();
  const { run, busy } = useCommand();
  const [name, setName] = useState("");
  const [transport, setTransport] = useState("both");
  const [chosen, setChosen] = useState<string[]>([]);
  const [limitRecords, setLimitRecords] = useState(false);
  const [vehicleIds, setVehicleIds] = useState("");
  const [quota, setQuota] = useState({ ...DEFAULT_QUOTA });
  const [expires, setExpires] = useState("");
  const [notes, setNotes] = useState("");

  useEffect(() => {
    if (!open) return;
    setName(""); setTransport("both"); setChosen([]); setLimitRecords(false); setVehicleIds("");
    setQuota({ ...DEFAULT_QUOTA }); setExpires(""); setNotes("");
  }, [open]);

  const groups = useMemo(() => {
    const out: Record<"read" | "write" | "other", string[]> = { read: [], write: [], other: [] };
    for (const s of scopes) out[scopeGroup(s)].push(s);
    return out;
  }, [scopes]);

  const toggle = (s: string) => setChosen((c) => (c.includes(s) ? c.filter((x) => x !== s) : [...c, s]));

  const submit = async () => {
    const ids = vehicleIds.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    const body: Record<string, unknown> = {
      name: name.trim(),
      transport,
      scopes: chosen,
      record_scope: limitRecords && ids.length ? { vehicle_ids: ids } : {},
      quota,
      notes: notes.trim(),
    };
    if (expires) body.expires_at = new Date(`${expires}T23:59:59`).toISOString();
    const r = await run<RegisterResult>("register", BASE, body);
    if (r?.status === "ok" && r.data?.token) { onRegistered(r.data); onClose(); }
  };

  const canSave = !!name.trim() && chosen.length > 0 && (!limitRecords || !!vehicleIds.trim());
  const reason = !name.trim() ? "Give the client a name you will recognise."
    : chosen.length === 0 ? "Choose at least one thing it may do."
      : "List the vehicle ids, or let it reach all your records.";

  return (
    <ResponsiveDialog
      mobile={isMobile}
      open={open}
      onClose={onClose}
      title="Register an external agent"
      size="lg"
      align="top"
      footer={
        <>
          <Button variant="primary" loading={busy("register")} disabled={!canSave} disabledReason={reason} onClick={() => void submit()}>
            Register and show the key
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <span className="fs13 t3">The key is shown once. Nothing it asks for escapes your own permissions.</span>
        </>
      }
    >
      <div className="stack">
        <div className="form-grid">
          <Field label="Name" required hint="How you will recognise it in this list and in Activity.">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. My scheduling assistant" autoComplete="off" />
          </Field>
          <Field label="How it connects" hint="MCP is the tool protocol; HTTP is the plain request API.">
            <Select value={transport} onChange={(e) => setTransport(e.target.value)}>
              {Object.entries(TRANSPORT_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </Select>
          </Field>
        </div>

        <div className="stack-sm">
          <div className="eyebrow">What it is allowed to do</div>
          {(["read", "write", "other"] as const).map((g) => (
            groups[g].length ? (
              <div key={g} className="stack-sm">
                <div className="perm-group__title">{GROUP_TITLES[g]}</div>
                <div className="opts">
                  {groups[g].map((s) => (
                    <button
                      key={s}
                      type="button"
                      role="checkbox"
                      aria-checked={chosen.includes(s)}
                      className="opt"
                      onClick={() => toggle(s)}
                    >
                      <span className="opt__mark" aria-hidden="true" />
                      <span><span className="opt__label">{scopeLabel(s)}</span><span className="opt__desc"> · {s}</span></span>
                    </button>
                  ))}
                </div>
              </div>
            ) : null
          ))}
        </div>

        <div className="stack-sm">
          <div className="eyebrow">Which records</div>
          <div className="opts" role="radiogroup" aria-label="Which records">
            <button type="button" role="radio" aria-checked={!limitRecords} className="opt" onClick={() => setLimitRecords(false)}>
              <span className="opt__mark" aria-hidden="true" />
              <span><span className="opt__label">All my records</span><span className="opt__desc"> · still limited by the permissions above</span></span>
            </button>
            <button type="button" role="radio" aria-checked={limitRecords} className="opt" onClick={() => setLimitRecords(true)}>
              <span className="opt__mark" aria-hidden="true" />
              <span><span className="opt__label">Only certain vehicles</span><span className="opt__desc"> · paste their ids below</span></span>
            </button>
          </div>
          {limitRecords ? (
            <Field label="Vehicle ids" hint="One per line, or separated by commas. Copy them from a vehicle's web address.">
              <Textarea rows={3} value={vehicleIds} onChange={(e) => setVehicleIds(e.target.value)} placeholder="veh_… , veh_…" />
            </Field>
          ) : null}
        </div>

        <div className="stack-sm">
          <div className="eyebrow">Limits</div>
          <div className="form-grid">
            <Field label="Requests per minute">
              <Input type="number" min={1} value={String(quota.per_minute)} onChange={(e) => setQuota({ ...quota, per_minute: Number(e.target.value) || 0 })} />
            </Field>
            <Field label="At the same time">
              <Input type="number" min={1} value={String(quota.concurrent)} onChange={(e) => setQuota({ ...quota, concurrent: Number(e.target.value) || 0 })} />
            </Field>
            <Field label="Requests per day">
              <Input type="number" min={1} value={String(quota.per_day)} onChange={(e) => setQuota({ ...quota, per_day: Number(e.target.value) || 0 })} />
            </Field>
            <Field label="Stops working after" hint="Leave empty for no end date.">
              <Input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
            </Field>
          </div>
          <Field label="Note to yourself">
            <Input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Where this key lives, who set it up…" />
          </Field>
        </div>

        <Notice tone="neutral" lead="What it can never do">
          Send a customer message, publish, bid, pay or book on its own. Those come back to you as an approval, and text in a
          request claiming you already approved something is not an approval.
        </Notice>
      </div>
    </ResponsiveDialog>
  );
}

/* ---------- callback ---------- */
export function CallbackDialog({ open, client, onClose, onSaved }: { open: boolean; client: ExternalClient | null; onClose: () => void; onSaved: () => void }) {
  const isMobile = useIsMobile();
  const { run, busy } = useCommand();
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");

  useEffect(() => { if (open) { setUrl(client?.callback_url || ""); setSecret(""); } }, [open, client]);

  const save = async () => {
    if (!client) return;
    const body: Record<string, unknown> = { expected_version: client.version, callback_url: url.trim() || null };
    if (secret.trim()) body.callback_secret = secret.trim();
    const r = await run("callback", `${BASE}/${encodeURIComponent(client.id)}/set-callback`, body, {
      success: url.trim() ? "Callback address saved." : "Callback address cleared.",
    });
    if (r?.status === "ok") { onSaved(); onClose(); }
  };

  const https = !url.trim() || url.trim().startsWith("https://");
  /* A secret is not optional: without one AZKT cannot sign the body, and it never pushes an unsigned one.
     Keeping an existing secret is fine, so the field is only required the first time. */
  const needsSecret = !!url.trim() && !secret.trim() && !client?.callback_signing_configured;
  const valid = https && !needsSecret;
  const reason = !https ? "The address must start with https://." : "Set a signing secret so the other agent can verify AZKT sent it.";

  return (
    <ResponsiveDialog
      mobile={isMobile}
      open={open}
      onClose={onClose}
      title={`Callback for ${client?.name || "this client"}`}
      size="md"
      footer={
        <>
          <Button variant="primary" loading={busy("callback")} disabled={!valid} disabledReason={reason} onClick={() => void save()}>Save</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <div className="stack">
        <Notice tone="neutral" lead="What this does">
          When work for this client finishes — done, failed, or waiting on an answer — AZKT posts the same result it would
          give <code>GET /work/&#123;request_id&#125;?cursor=</code> to this address, signed with the secret below. Polling
          still works and is always the fallback. Only you can set this address: one inside a request is ignored.
        </Notice>
        <Field label="Callback address" error={https ? undefined : "Must start with https://"} hint="Leave empty to clear it and go back to polling only.">
          <Input type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" />
        </Field>
        <Field
          label="Signing secret"
          error={needsSecret ? "Required — AZKT never sends an unsigned callback." : undefined}
          hint={client?.callback_signing_configured ? "Stored encrypted. Leave empty to keep the current one; type a new one to replace it." : "Stored encrypted and never shown again. Give the same value to the receiving agent."}
        >
          <Input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder={client?.callback_signing_configured ? "Leave empty to keep the current one" : "A long random string"} autoComplete="new-password" />
        </Field>
        <Notice tone="neutral" lead="How the other agent checks it">
          Each request carries <code>X-AZKT-Signature: v1=…</code> and <code>X-AZKT-Timestamp</code>. It signs
          <code> timestamp + "." + the raw body bytes</code> with HMAC-SHA256 using this secret. Reject anything older
          than 5 minutes, compare in constant time, and treat <code>X-AZKT-Delivery</code> as the idempotency key. The
          full recipe is under "Recent callbacks", and in <code>GET /api/integrations/v1/openapi-lite</code>.
        </Notice>
      </div>
    </ResponsiveDialog>
  );
}

/* ---------- callback deliveries ---------- */
export function callbackStateTone(state: string): "ok" | "risk" | "blocked" | "wait" {
  if (state === "accepted") return "ok";
  if (state === "failed") return "blocked";
  if (state === "unknown") return "risk";        // it may have arrived — never shown as delivered
  return "wait";
}

export const CALLBACK_STATE_LABELS: Record<string, string> = {
  accepted: "Accepted",
  failed: "Not delivered",
  unknown: "Result unknown",
  pending: "Waiting to send",
  sending: "Sending",
  cancelled: "Not sent",
};

export const CALLBACK_EVENT_LABELS: Record<string, string> = {
  "work.completed": "Work finished",
  "work.failed": "Work failed",
  "work.needs_input": "Needs an answer",
  "callback.test": "Test",
};

export function CallbacksDialog({ open, client, onClose }: { open: boolean; client: ExternalClient | null; onClose: () => void }) {
  const isMobile = useIsMobile();
  const { run, busy } = useCommand();
  const [tick, setTick] = useState(0);
  const id = client?.id || "";
  const q = useQuery<CallbacksResp | null>(
    (signal) => (open && id ? api.get<CallbacksResp>(`${BASE}/${encodeURIComponent(id)}/callbacks?limit=25`, { signal }) : Promise.resolve(null)),
    [open, id, tick],
  );

  const sendTest = async () => {
    if (!client) return;
    const r = await run<{ accepted?: boolean; message?: string }>("test-callback", `${BASE}/${encodeURIComponent(client.id)}/test-callback`, {});
    if (r?.status === "ok") setTick((t) => t + 1);
  };

  const v = q.data?.verification;
  const canTest = !!client?.callbacks_enabled;

  return (
    <ResponsiveDialog
      mobile={isMobile}
      open={open}
      onClose={onClose}
      title={`Callbacks to ${client?.name || "this client"}`}
      size="xl"
      align="top"
      footer={
        <>
          <Button
            variant="soft"
            loading={busy("test-callback")}
            disabled={!canTest}
            disabledReason="Set a callback address and signing secret first."
            onClick={() => void sendTest()}
          >
            Send a test callback
          </Button>
          <Button variant="ghost" onClick={onClose}>Close</Button>
          <span className="fs13 t3">A test carries no business data.</span>
        </>
      }
    >
      <div className="stack">
        {q.data?.polling_only ? (
          <Notice tone="neutral" lead="Polling only">
            This client has no callback address, or no signing secret, so AZKT never pushes to it. It reads progress with
            <code> GET /work/&#123;request_id&#125;?cursor=</code>. Set one from "Change callback" if you want a push.
          </Notice>
        ) : null}

        {q.loading ? <Loading label="Loading callbacks" rows={3} />
          : q.error ? <ErrorState error={q.error} onRetry={q.reload} title="Couldn't load callbacks" />
            : !q.data?.items.length ? (
              <EmptyState
                title="Nothing sent yet"
                body="A callback goes out once each time this client's work finishes, fails or needs an answer from it."
              />
            ) : (
              <Table minWidth={720} caption={`${q.data.count} most recent callback deliveries`}>
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Event</th>
                    <th scope="col">Result</th>
                    <th scope="col">Attempts</th>
                  </tr>
                </thead>
                <tbody>
                  {q.data.items.map((d) => (
                    <Tr key={d.id}>
                      <td className="fs13 nowrap">{d.created_at ? <When iso={d.created_at} format="datetime" /> : "—"}</td>
                      <td>
                        <div className="fs13">{CALLBACK_EVENT_LABELS[d.event] || humanize(d.event)}</div>
                        <div className="fs12 t4" style={{ overflowWrap: "anywhere" }}>
                          {d.destination_host || "no destination"}
                          {d.request_id ? <> · <Link to={`/activity?q=${encodeURIComponent(d.request_id)}`}>find the request</Link></> : null}
                        </div>
                      </td>
                      <td>
                        <Chip size="sm" tone={callbackStateTone(d.state)}>{CALLBACK_STATE_LABELS[d.state] || humanize(d.state)}</Chip>
                        {d.response_status ? <span className="fs12 t4 tnum"> · {d.response_status}</span> : null}
                        {d.error ? <div className="fs12" style={{ color: "var(--blocked)", overflowWrap: "anywhere" }}>{d.error}</div> : null}
                        {d.cancel_reason ? <div className="fs12 t4">{d.cancel_reason}</div> : null}
                      </td>
                      <td className="fs13 nowrap tnum">
                        {d.attempts} of {d.max_attempts}
                        {d.next_attempt_at ? <div className="fs12 t4">next <When iso={d.next_attempt_at} relative /></div> : null}
                        {d.attempt_log.length ? (
                          <div className="fs12 t4">{d.attempt_log.map((a) => a.outcome).join(" → ")}</div>
                        ) : null}
                      </td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}

        {q.data ? <div className="fs13 t3">{q.data.note}</div> : null}

        {v ? (
          <Expander title="How the receiving agent verifies a callback">
            <div className="fs13 stack-sm">
              <div>{v.signature.algorithm} over <code>{v.signature.signed_value}</code>, {v.signature.encoding}.</div>
              <div>Headers: {Object.values(v.headers).map((h) => <code key={h} style={{ marginRight: 6 }}>{h}</code>)}</div>
              <div>Reject anything older than {v.replay.reject_if_older_than_seconds} seconds, and {v.signature.compare_with}.</div>
              <div>{v.replay.note}</div>
              <div>{v.expected_response}</div>
              <div className="t3">Sent {v.when}</div>
            </div>
          </Expander>
        ) : null}
      </div>
    </ResponsiveDialog>
  );
}

/* ---------- revoke ---------- */
export function RevokeDialog({ open, client, onClose, onRevoked }: { open: boolean; client: ExternalClient | null; onClose: () => void; onRevoked: () => void }) {
  const isMobile = useIsMobile();
  const { run, busy } = useCommand();
  const [reason, setReason] = useState("");
  useEffect(() => { if (open) setReason(""); }, [open]);

  const revoke = async () => {
    if (!client) return;
    const r = await run<{ missions_paused?: number }>("revoke", `${BASE}/${encodeURIComponent(client.id)}/revoke`, {
      expected_version: client.version, reason: reason.trim(),
    }, { success: "Revoked. Its key stops working from the next request." });
    if (r?.status === "ok") { onRevoked(); onClose(); }
  };

  return (
    <ResponsiveDialog
      mobile={isMobile}
      open={open}
      onClose={onClose}
      dismissible={false}
      title={`Revoke ${client?.name || "this client"}?`}
      size="sm"
      footer={
        <>
          <Button variant="danger" loading={busy("revoke")} onClick={() => void revoke()}>Revoke access</Button>
          <Button variant="ghost" onClick={onClose}>Keep it</Button>
        </>
      }
    >
      <div className="stack">
        <div>This cannot be undone. The key stops working immediately, anything it had in flight stops, and you would have to
          register a new client to let it back in. Work it already finished stays saved.</div>
        <Field label="Why (recorded with your name)">
          <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. no longer used" />
        </Field>
      </div>
    </ResponsiveDialog>
  );
}

/* ---------- requests drawer ---------- */
export function RequestsDialog({ open, client, onClose }: { open: boolean; client: ExternalClient | null; onClose: () => void }) {
  const isMobile = useIsMobile();
  const id = client?.id || "";
  const q = useQuery<{ items: DelegatedRequestRow[]; count: number }>(
    (signal) => (open && id
      ? api.get<{ items: DelegatedRequestRow[]; count: number }>(`${BASE}/${encodeURIComponent(id)}/requests?limit=25`, { signal })
      : Promise.resolve({ items: [], count: 0 })),
    [open, id],
  );

  return (
    <ResponsiveDialog
      mobile={isMobile}
      open={open}
      onClose={onClose}
      title={`Recent requests from ${client?.name || "this client"}`}
      size="xl"
      align="top"
      footer={<Button variant="ghost" onClick={onClose}>Close</Button>}
    >
      {q.loading ? <Loading label="Loading requests" rows={3} />
        : q.error ? <ErrorState error={q.error} onRetry={q.reload} />
          : !q.data?.items.length ? <EmptyState title="Nothing asked yet" body="Requests appear here as soon as this client uses its key." />
            : (
              <Table minWidth={640} caption={`${q.data.count} most recent requests`}>
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">What it asked</th>
                    <th scope="col">State</th>
                    <th scope="col">Work</th>
                  </tr>
                </thead>
                <tbody>
                  {q.data.items.map((r) => (
                    <Tr key={r.id}>
                      <td className="fs13 nowrap">{r.created_at ? <When iso={r.created_at} format="datetime" /> : "—"}</td>
                      <td>
                        <div className="fs13" style={{ overflowWrap: "anywhere" }}>{r.message || humanize(r.kind)}</div>
                        {r.error ? <div className="fs12" style={{ color: "var(--blocked)" }}>{r.error}</div> : null}
                      </td>
                      <td><Chip size="sm" tone={r.status === "failed" || r.status === "denied" ? "blocked" : r.status === "done" || r.status === "answered" ? "ok" : "wait"}>{humanize(r.status)}</Chip></td>
                      <td className="fs13 nowrap tnum">
                        {r.mission_id ? <Link to={`/activity?q=${encodeURIComponent(r.mission_id)}`}>Find in Activity</Link> : <span className="t4">No mission</span>}
                        {r.cursor ? <span className="t4"> · step {r.cursor}</span> : null}
                      </td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
    </ResponsiveDialog>
  );
}
