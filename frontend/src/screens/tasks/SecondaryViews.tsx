/* Tasks › Cases and Tasks › Promises — the two secondary views of the Tasks screen.
   Cases:    GET /api/tasks/cases?status=open|all    (backend/app/routers/tasks.py list_cases)
   Promises: GET /api/tasks/promises?status=open|all (backend/app/routers/tasks.py list_promises)
   Every row links to the record it is about and nothing is invented — a case with no next check says
   so, and a promise whose person the role cannot see stays unnamed.

   Both rows carry the actions that resolve them (SecondaryActions.tsx). The server decides what is
   allowed: a case cannot be resolved without a summary and a promise cannot be closed as missed or
   withdrawn without a note, and a refusal is shown rather than worked around. */
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { entityHref } from "../../lib/links";
import { TZ } from "../../lib/format";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useCommand } from "../../lib/useCommand";
import { Button, Chip, EmptyState, ErrorState, GlassPanel, HealthLabel, Loading, When } from "../../ui";
import type { useNames } from "./useNames";
import {
  CancelCaseSheet, CaseActions, CaseCheckSheet, ClosePromiseSheet, PromiseActions, PromiseDateSheet,
  ResolveCaseSheet, casePath, promisePath,
} from "./SecondaryActions";
import {
  CASE_KIND_LABEL, CASE_STATUS_LABEL, PROMISE_STATUS_LABEL, caseHealth, promiseHealth,
  type CaseListResp, type CaseView, type PromiseListResp, type PromiseView,
} from "./types";

type Names = ReturnType<typeof useNames>;

interface RecordLink { href: string; label: string }

/** The record a case is about, in the order a person would look for it. */
function caseRecord(c: CaseView, names: Names): RecordLink | null {
  if (c.vehicle_id) return { href: `/vehicles/${encodeURIComponent(c.vehicle_id)}`, label: names.vehicleName(c.vehicle_id) };
  const shipment = entityHref("shipment", c.shipment_id);
  if (shipment) return { href: shipment, label: "Shipment" };
  const request = entityHref("import_request", c.import_request_id);
  if (request) return { href: request, label: "Import request" };
  const thread = entityHref("conversation", c.conversation_id);
  if (thread) return { href: thread, label: "Email thread" };
  if (c.opportunity_id) return { href: `/sales?lead=${encodeURIComponent(c.opportunity_id)}`, label: names.leadName(c.opportunity_id) };
  return null;
}

function CaseRow({ c, names, tokyo, actions }: {
  c: CaseView; names: Names; tokyo: boolean; actions: ReactNode;
}) {
  const health = caseHealth(c);
  const record = caseRecord(c, names);
  const overdue = c.overdue_check && c.status !== "resolved" && c.status !== "cancelled";
  return (
    <div className={["tk-row", overdue ? "tk-row--overdue" : ""].filter(Boolean).join(" ")}>
      <div className="tk-row__main">
        <div className="tk-row__title">
          <Chip size="sm" tone="soft">{CASE_KIND_LABEL[c.kind] || c.kind.replace(/_/g, " ")}</Chip>
          <span>{c.title}</span>
          {health ? <HealthLabel health={health.health} label={health.label} /> : <span className="fs12 t4">{CASE_STATUS_LABEL[c.status] || c.status}</span>}
        </div>
        <div className="tk-row__meta">
          {record ? <Link to={record.href}>{record.label}</Link> : <span className="t4">No record linked</span>}
          <span>· Waiting on {c.waiting_on || "nobody in particular"}</span>
          {c.summary ? <span className="truncate" style={{ maxWidth: 360 }}>· {c.summary}</span> : null}
        </div>
        <div className="tk-row__meta">
          {c.next_action ? <span>Next: {c.next_action}</span> : <span className="t4">No next action written down</span>}
        </div>
      </div>
      <div className="tk-row__side">
        <span className={["tk-row__when", overdue ? "tk-row__when--overdue" : ""].filter(Boolean).join(" ")}>
          {c.next_check_at
            ? <>{overdue ? "Check overdue · " : "Check "}<When iso={c.next_check_at} tz={TZ.phoenix} withTokyo={tokyo} /></>
            : <span className="t4">No next check</span>}
        </span>
        <span className="fs12 t4">{c.owner_role ? `Owned by ${c.owner_role}` : "No owner recorded"}</span>
        <div className="tk-row__actions">
          {record ? <Button size="xs" variant="soft" to={record.href}>Open</Button> : null}
          {actions}
        </div>
      </div>
    </div>
  );
}

export function CasesView({ data, loading, error, reload, names, tokyo, showClosed }: {
  data: CaseListResp | null; loading: boolean; error: unknown; reload: () => void;
  names: Names; tokyo: boolean; showClosed: boolean;
}) {
  const { user } = useAuth();
  const { run } = useCommand();
  const writable = can(user, "tasks.write");
  const [resolve, setResolve] = useState<CaseView | null>(null);
  const [check, setCheck] = useState<CaseView | null>(null);
  const [cancel, setCancel] = useState<CaseView | null>(null);
  // Status-only moves need no extra input, so they post straight from the row.
  const move = async (c: CaseView, action: string) => {
    const r = await run(`case:${action}:${c.id}`, casePath(c.id, action), { expected_version: c.version },
      { success: action === "reopen" ? "Case reopened" : "Case updated" });
    if (r?.status === "ok") reload();
  };
  const handlers = { resolve: setResolve, check: setCheck, cancel: setCancel, status: move };
  if (loading && !data) return <GlassPanel clip><Loading label="Loading cases" rows={4} /></GlassPanel>;
  if (error) return <ErrorState error={error} onRetry={reload} title="Couldn't load cases" />;
  if (data === null) {
    return (
      <GlassPanel clip>
        <EmptyState title="Cases aren't available to your role" body="A case is a piece of work waiting on someone else. Ask the owner if you need to see them." />
      </GlassPanel>
    );
  }
  const rows = data.items || [];
  return (
    <section className="tk-group">
      <div className="section-title"><h2>Cases <span className="count">{rows.length}</span></h2></div>
      <GlassPanel>
        {rows.length === 0 ? (
          <EmptyState
            title={showClosed ? "No cases at all yet" : "Nothing is waiting on anyone"}
            body={showClosed ? "A case appears when a piece of work has to wait on someone else." : "Closed cases are hidden. Turn on “Include closed” to see them."}
          />
        ) : rows.map((c) => (
          <CaseRow key={c.id} c={c} names={names} tokyo={tokyo}
            actions={<CaseActions c={c} can={writable} whyNot={whyNot("tasks.write")} on={handlers} />} />
        ))}
      </GlassPanel>
      <ResolveCaseSheet row={resolve} open={!!resolve} onClose={() => setResolve(null)} onDone={reload} />
      <CaseCheckSheet row={check} open={!!check} onClose={() => setCheck(null)} onDone={reload}
        userTz={user?.timezone} />
      <CancelCaseSheet row={cancel} open={!!cancel} onClose={() => setCancel(null)} onDone={reload} />
    </section>
  );
}

function PromiseRow({ p, names, tokyo, actions }: {
  p: PromiseView; names: Names; tokyo: boolean; actions: ReactNode;
}) {
  const health = promiseHealth(p);
  const overdue = p.overdue && (p.status === "open" || p.status === "proposed");
  // contact_name is null for a role that cannot see contacts: no name, no link to a person.
  const person = p.contact_name;
  return (
    <div className={["tk-row", overdue ? "tk-row--overdue" : ""].filter(Boolean).join(" ")}>
      <div className="tk-row__main">
        <div className="tk-row__title">
          <Chip size="sm" tone="soft">Promise</Chip>
          <span>{p.text}</span>
          {health ? <HealthLabel health={health.health} label={health.label} /> : <span className="fs12 t4">{PROMISE_STATUS_LABEL[p.status] || p.status}</span>}
        </div>
        <div className="tk-row__meta">
          {person && p.contact_id
            ? <Link to={`/contacts/${encodeURIComponent(p.contact_id)}`}>{person}</Link>
            : <span className="t4">{p.contact_id ? "Person hidden for your role" : "Not promised to anyone in particular"}</span>}
          {p.vehicle_id ? <>
            <span aria-hidden="true">·</span>
            <Link to={`/vehicles/${encodeURIComponent(p.vehicle_id)}`}>{names.vehicleName(p.vehicle_id)}</Link>
          </> : null}
          {p.opportunity_id ? <>
            <span aria-hidden="true">·</span>
            <Link to={`/sales?lead=${encodeURIComponent(p.opportunity_id)}`}>{names.leadName(p.opportunity_id)}</Link>
          </> : null}
        </div>
      </div>
      <div className="tk-row__side">
        <span className={["tk-row__when", overdue ? "tk-row__when--overdue" : ""].filter(Boolean).join(" ")}>
          {p.due_at
            ? <>{overdue ? "Past due · " : "Due "}<When iso={p.due_at} tz={TZ.phoenix} withTokyo={tokyo} /></>
            : <span className="t4">No date promised</span>}
        </span>
        <span className="fs12 t4">
          {p.made_at ? <>Promised <When iso={p.made_at} tz={TZ.phoenix} relative /></> : "When it was promised isn't recorded"}
        </span>
        <div className="tk-row__actions">{actions}</div>
      </div>
    </div>
  );
}

export function PromisesView({ data, loading, error, reload, names, tokyo, showClosed }: {
  data: PromiseListResp | null; loading: boolean; error: unknown; reload: () => void;
  names: Names; tokyo: boolean; showClosed: boolean;
}) {
  const { user } = useAuth();
  const { run } = useCommand();
  const writable = can(user, "tasks.write");
  const [closing, setClosing] = useState<{ row: PromiseView; action: "missed" | "withdraw" } | null>(null);
  const [edit, setEdit] = useState<PromiseView | null>(null);
  // Kept and reopen carry no extra information, so they post straight from the row. Missed and
  // withdrawn open a sheet because the server requires a note saying what happened.
  const move = async (p: PromiseView, action: string) => {
    const r = await run(`promise:${action}:${p.id}`, promisePath(p.id, action), { expected_version: p.version },
      { success: action === "kept" ? "Recorded as kept" : "Promise reopened" });
    if (r?.status === "ok") reload();
  };
  const handlers = {
    close: (row: PromiseView, action: "missed" | "withdraw") => setClosing({ row, action }),
    edit: setEdit,
    simple: move,
  };
  if (loading && !data) return <GlassPanel clip><Loading label="Loading promises" rows={4} /></GlassPanel>;
  if (error) return <ErrorState error={error} onRetry={reload} title="Couldn't load promises" />;
  if (data === null) {
    return (
      <GlassPanel clip>
        <EmptyState title="Promises aren't available to your role" body="These are the commitments made to people. Ask the owner if you need to see them." />
      </GlassPanel>
    );
  }
  const rows = data.items || [];
  return (
    <section className="tk-group">
      <div className="section-title"><h2>Promises <span className="count">{rows.length}</span></h2></div>
      <GlassPanel>
        {rows.length === 0 ? (
          <EmptyState
            title={showClosed ? "No promises recorded yet" : "Nothing has been promised"}
            body={showClosed ? "A promise is recorded from a confirmed reply or written down by a person." : "Kept and missed promises are hidden. Turn on “Include closed” to see them."}
          />
        ) : rows.map((p) => (
          <PromiseRow key={p.id} p={p} names={names} tokyo={tokyo}
            actions={<PromiseActions p={p} can={writable} whyNot={whyNot("tasks.write")} on={handlers} />} />
        ))}
      </GlassPanel>
      <ClosePromiseSheet row={closing?.row || null} action={closing?.action || "missed"} open={!!closing}
        onClose={() => setClosing(null)} onDone={reload} />
      <PromiseDateSheet row={edit} open={!!edit} onClose={() => setEdit(null)} onDone={reload}
        userTz={user?.timezone} />
    </section>
  );
}
