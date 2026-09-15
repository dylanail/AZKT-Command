/* Actions on the Tasks › Cases and Tasks › Promises rows.

   Both lists used to be read-only, so a case could be watched but never closed from the screen it
   lives on and a promise could never be marked kept at all. Every action here posts to the same
   command the agent and the MCP connector call:

     POST /api/tasks/cases/{id}/{resolve|reopen|waiting|block|needs_owner|cancel|update}
     POST /api/tasks/promises/{id}/{kept|missed|withdraw|reopen|update}

   The server owns the rules — resolving a case needs a summary or evidence, closing a promise as
   missed or withdrawn needs a note — and a refusal is shown rather than worked around. */
import { useEffect, useState, type FormEvent } from "react";
import { useCommand } from "../../lib/useCommand";
import { TZ, type TzName } from "../../lib/format";
import { Button, Field, Input, Menu, ResponsiveDialog, Select, Textarea, type MenuItem } from "../../ui";
import { useIsMobile } from "../../lib/viewport";
import { defaultTz, localParts, zonedToUtc } from "./types";
import type { CaseView, PromiseView } from "./types";

export const casePath = (id: string, action: string) => `/api/tasks/cases/${encodeURIComponent(id)}/${action}`;
export const promisePath = (id: string, action: string) => `/api/tasks/promises/${encodeURIComponent(id)}/${action}`;

const CASE_CLOSED = new Set(["resolved", "cancelled"]);
const PROMISE_CLOSED = new Set(["met", "missed", "withdrawn"]);

/* ---------- a date+time+zone the server can read ---------- */
interface WhenValue { date: string; time: string; tz: TzName }

function whenFrom(iso: string | null, userTz?: string | null): WhenValue {
  const tz = (iso ? TZ.phoenix : defaultTz(userTz)) as TzName;
  if (!iso) return { date: "", time: "09:00", tz };
  const p = localParts(iso, tz);
  return { date: p.date, time: p.time, tz };
}

function WhenFields({ value, onChange, label }: { value: WhenValue; onChange: (v: WhenValue) => void; label: string }) {
  return (
    <div className="row-wrap">
      <Field label={label}>
        <Input type="date" value={value.date} onChange={(e) => onChange({ ...value, date: e.target.value })} />
      </Field>
      <Field label="Time">
        <Input type="time" value={value.time} onChange={(e) => onChange({ ...value, time: e.target.value })} />
      </Field>
      <Field label="Zone">
        <Select value={value.tz} onChange={(e) => onChange({ ...value, tz: e.target.value as TzName })}>
          <option value={TZ.phoenix}>Phoenix</option>
          <option value={TZ.tokyo}>Tokyo</option>
        </Select>
      </Field>
    </div>
  );
}

function whenPayload(v: WhenValue, key: string): Record<string, unknown> | null {
  if (!v.date) return null;
  const inst = zonedToUtc(v.date, v.time || "09:00", v.tz);
  return inst ? { [key]: inst.toISOString(), timezone: v.tz } : null;
}

/* ---------- Case: resolve ---------- */
export function ResolveCaseSheet({ row, open, onClose, onDone }: {
  row: CaseView | null; open: boolean; onClose: () => void; onDone: () => void;
}) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [summary, setSummary] = useState("");
  useEffect(() => { if (open) setSummary(row?.summary || ""); }, [open, row]);
  if (!row) return null;
  const key = `case:resolve:${row.id}`;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await run(key, casePath(row.id, "resolve"),
      { summary: summary.trim(), expected_version: row.version }, { success: "Case resolved" });
    if (r?.status === "ok") { onDone(); onClose(); }
  };
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="Resolve case" description={row.title}
      footer={<>
        <Button type="submit" form="tk-case-resolve" variant="primary" loading={busy(key)}
          disabled={!summary.trim()} disabledReason="Say how it ended before closing it.">Resolve</Button>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
      </>}>
      <form id="tk-case-resolve" onSubmit={submit}>
        <Field label="How did it end?" hint="Recorded on the case. A case is never closed without saying what happened.">
          <Textarea rows={4} value={summary} onChange={(e) => setSummary(e.target.value)}
            placeholder="MOL quoted 210,000 JPY, booked for the 20th" />
        </Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- Case: next check / waiting on ---------- */
export function CaseCheckSheet({ row, open, onClose, onDone, userTz }: {
  row: CaseView | null; open: boolean; onClose: () => void; onDone: () => void; userTz?: string | null;
}) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [when, setWhen] = useState<WhenValue>(whenFrom(null, userTz));
  const [action, setAction] = useState("");
  const [waitingOn, setWaitingOn] = useState("");
  useEffect(() => {
    if (!open || !row) return;
    setWhen(whenFrom(row.next_check_at, userTz));
    setAction(row.next_action || "");
    setWaitingOn(row.waiting_on || "");
  }, [open, row, userTz]);
  if (!row) return null;
  const key = `case:check:${row.id}`;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const body: Record<string, unknown> = { expected_version: row.version, ...(whenPayload(when, "next_check_at") || {}) };
    if (action.trim()) body.next_action = action.trim();
    if (waitingOn.trim()) body.waiting_on = waitingOn.trim();
    const r = await run(key, casePath(row.id, "update"), body, { success: "Case updated" });
    if (r?.status === "ok") { onDone(); onClose(); }
  };
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="Next check" description={row.title}
      footer={<>
        <Button type="submit" form="tk-case-check" variant="primary" loading={busy(key)}>Save</Button>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
      </>}>
      <form id="tk-case-check" onSubmit={submit} className="stack">
        <WhenFields value={when} onChange={setWhen} label="Check again on" />
        <Field label="Next action" hint="What happens at that check.">
          <Input value={action} onChange={(e) => setAction(e.target.value)} placeholder="Chase the broker for the quote" />
        </Field>
        <Field label="Waiting on">
          <Input value={waitingOn} onChange={(e) => setWaitingOn(e.target.value)} placeholder="MOL" />
        </Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- Case: cancel ---------- */
export function CancelCaseSheet({ row, open, onClose, onDone }: {
  row: CaseView | null; open: boolean; onClose: () => void; onDone: () => void;
}) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [why, setWhy] = useState("");
  useEffect(() => { if (open) setWhy(""); }, [open]);
  if (!row) return null;
  const key = `case:cancel:${row.id}`;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await run(key, casePath(row.id, "cancel"),
      { summary: why.trim() || undefined, expected_version: row.version }, { success: "Case cancelled" });
    if (r?.status === "ok") { onDone(); onClose(); }
  };
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="Cancel case" description={row.title}
      footer={<>
        <Button type="submit" form="tk-case-cancel" variant="primary" loading={busy(key)}>Cancel case</Button>
        <Button variant="ghost" onClick={onClose}>Keep it open</Button>
      </>}>
      <form id="tk-case-cancel" onSubmit={submit}>
        <Field label="Why is it being dropped?" hint="Kept on the record so the history still makes sense.">
          <Textarea rows={3} value={why} onChange={(e) => setWhy(e.target.value)} placeholder="The customer bought elsewhere" />
        </Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- Promise: close with a note (missed / withdrawn) ---------- */
export function ClosePromiseSheet({ row, action, open, onClose, onDone }: {
  row: PromiseView | null; action: "missed" | "withdraw"; open: boolean; onClose: () => void; onDone: () => void;
}) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [note, setNote] = useState("");
  useEffect(() => { if (open) setNote(""); }, [open, action]);
  if (!row) return null;
  const key = `promise:${action}:${row.id}`;
  const missed = action === "missed";
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await run(key, promisePath(row.id, action), { note: note.trim(), expected_version: row.version },
      { success: missed ? "Recorded as missed" : "Promise withdrawn" });
    if (r?.status === "ok") { onDone(); onClose(); }
  };
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose}
      title={missed ? "Mark this promise missed" : "Withdraw this promise"} description={row.text}
      footer={<>
        <Button type="submit" form="tk-promise-close" variant="primary" loading={busy(key)}
          disabled={!note.trim()} disabledReason="Say what happened first.">
          {missed ? "Mark missed" : "Withdraw"}
        </Button>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
      </>}>
      <form id="tk-promise-close" onSubmit={submit}>
        <Field label="What happened?"
          hint={missed ? "The customer was told something and it did not happen; the reason stays on the record."
                       : "Why the promise no longer stands."}>
          <Textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)}
            placeholder={missed ? "Photos were not ready; told the customer on Monday" : "Customer cancelled the order"} />
        </Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- Promise: change what was promised / when ---------- */
export function PromiseDateSheet({ row, open, onClose, onDone, userTz }: {
  row: PromiseView | null; open: boolean; onClose: () => void; onDone: () => void; userTz?: string | null;
}) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [when, setWhen] = useState<WhenValue>(whenFrom(null, userTz));
  const [text, setText] = useState("");
  useEffect(() => {
    if (!open || !row) return;
    setWhen(whenFrom(row.due_at, userTz));
    setText(row.text);
  }, [open, row, userTz]);
  if (!row) return null;
  const key = `promise:update:${row.id}`;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const body: Record<string, unknown> = { expected_version: row.version, ...(whenPayload(when, "due_at") || {}) };
    if (text.trim() && text.trim() !== row.text) body.text = text.trim();
    const r = await run(key, promisePath(row.id, "update"), body, { success: "Promise updated" });
    if (r?.status === "ok") { onDone(); onClose(); }
  };
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="Change the promise" description={row.text}
      footer={<>
        <Button type="submit" form="tk-promise-date" variant="primary" loading={busy(key)}>Save</Button>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
      </>}>
      <form id="tk-promise-date" onSubmit={submit} className="stack">
        <Field label="What was promised">
          <Input value={text} onChange={(e) => setText(e.target.value)} />
        </Field>
        <WhenFields value={when} onChange={setWhen} label="By when" />
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- row menus ---------- */
export interface CaseHandlers {
  resolve: (c: CaseView) => void;
  check: (c: CaseView) => void;
  cancel: (c: CaseView) => void;
  status: (c: CaseView, action: string) => void;
}

export function CaseActions({ c, can, whyNot, on }: {
  c: CaseView; can: boolean; whyNot: string; on: CaseHandlers;
}) {
  const closed = CASE_CLOSED.has(c.status);
  const items: MenuItem[] = closed
    ? [{ label: "Reopen", onSelect: () => on.status(c, "reopen"), disabled: !can, disabledReason: whyNot }]
    : [
        { label: "Set next check…", onSelect: () => on.check(c), disabled: !can, disabledReason: whyNot },
        { label: "Waiting on someone", onSelect: () => on.status(c, "waiting"), disabled: !can || c.status === "waiting", disabledReason: whyNot },
        { label: "Blocked", onSelect: () => on.status(c, "block"), disabled: !can || c.status === "blocked", disabledReason: whyNot },
        { label: "Needs the owner", onSelect: () => on.status(c, "needs_owner"), disabled: !can || c.status === "needs_owner", disabledReason: whyNot },
        { label: "Cancel case…", sepBefore: true, onSelect: () => on.cancel(c), disabled: !can, disabledReason: whyNot },
      ];
  return (
    <>
      {closed ? null : (
        <Button size="xs" variant="primary" onClick={() => on.resolve(c)} disabled={!can} disabledReason={whyNot}>
          Resolve
        </Button>
      )}
      <Menu label={`Actions for ${c.title}`} align="right" items={items}
        trigger={<Button size="xs" variant="soft">More</Button>} />
    </>
  );
}

export interface PromiseHandlers {
  close: (p: PromiseView, action: "missed" | "withdraw") => void;
  edit: (p: PromiseView) => void;
  simple: (p: PromiseView, action: string) => void;
}

export function PromiseActions({ p, can, whyNot, on }: {
  p: PromiseView; can: boolean; whyNot: string; on: PromiseHandlers;
}) {
  const closed = PROMISE_CLOSED.has(p.status);
  const items: MenuItem[] = closed
    ? [{ label: "Reopen", onSelect: () => on.simple(p, "reopen"), disabled: !can, disabledReason: whyNot }]
    : [
        { label: "Change date or wording…", onSelect: () => on.edit(p), disabled: !can, disabledReason: whyNot },
        { label: "Mark missed…", onSelect: () => on.close(p, "missed"), disabled: !can, disabledReason: whyNot },
        { label: "Withdraw…", sepBefore: true, onSelect: () => on.close(p, "withdraw"), disabled: !can, disabledReason: whyNot },
      ];
  return (
    <>
      {closed ? null : (
        <Button size="xs" variant="primary" onClick={() => on.simple(p, "kept")} disabled={!can} disabledReason={whyNot}>
          Kept
        </Button>
      )}
      <Menu label={`Actions for this promise`} align="right" items={items}
        trigger={<Button size="xs" variant="soft">More</Button>} />
    </>
  );
}
