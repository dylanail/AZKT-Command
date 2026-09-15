/* Settings › External agents — shared shapes, plain-language labels and the four dialogs.
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
  Button, Chip, EmptyState, ErrorState, Field, Input, Loading, Notice, ResponsiveDialog, Select, Table, Textarea, Tr, When,
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
  owner_user_id: string;
  notes: string;
  version: number;
  created_at: string | null;
  rotated_from_id: string | null;
  health?: Record<string, unknown>;
  /** From GET /health only. */
  in_flight_missions?: number;
  requests_total?: number;
  state?: string;
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

  const valid = !url.trim() || url.trim().startsWith("https://");

  return (
    <ResponsiveDialog
      mobile={isMobile}
      open={open}
      onClose={onClose}
      title={`Callback for ${client?.name || "this client"}`}
      size="md"
      footer={
        <>
          <Button variant="primary" loading={busy("callback")} disabled={!valid} disabledReason="The address must start with https://." onClick={() => void save()}>Save</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <div className="stack">
        <Notice tone="neutral" lead="Polling today">
          AZKT records this address, but it does not push to it yet: clients read progress with
          <code> GET /work/&#123;request_id&#125;?cursor=</code> until that is built. Only you can set this — an address
          inside a request is ignored.
        </Notice>
        <Field label="Callback address" error={valid ? undefined : "Must start with https://"} hint="Leave empty to clear it.">
          <Input type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" />
        </Field>
        <Field label="Signing secret" hint="Optional. Stored encrypted and never shown again.">
          <Input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="Leave empty to keep the current one" autoComplete="new-password" />
        </Field>
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
