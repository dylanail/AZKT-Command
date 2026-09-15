/* Settings › Calendar. Calls, meetings and scheduled follow-ups become entries on the business
   Google Calendar (backend/app/services/calendar_sync.py).

   GET  /api/calendar/status          — connection, the owner capability, the rules, what is stuck
   GET  /api/calendar/today           — one local day: AZKT's appointments plus the calendar's own entries
   POST /api/calendar/connect         — owner; starts Google consent ({enable_write} also asks for the event scope)
   POST /api/calendar/writes          — owner; the capability switch itself (spec §11.2)
   POST /api/calendar/resync          — owner; queues durable jobs, never writes inside the request

   Two separate permissions, shown as two separate steps, because that is what the server enforces: a
   connected Google account does not let AZKT create anything until the owner turns writes on. */
import { useCallback, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, describeError } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { can, whyNot } from "../../../lib/perms";
import { useCommand } from "../../../lib/useCommand";
import { useQuery } from "../../../lib/useQuery";
import {
  Badge, Button, Chip, EmptyState, ErrorState, Expander, GlassPanel, Input, KeyValues, Loading,
  Notice, NotRecorded, Switch, When, useToast,
} from "../../../ui";
import type { Connection } from "./types";
import "../../../styles/notify.css";

/* ---- shapes (backend/app/services/calendar_sync.py) ---- */
interface Blockage { reason: string; setup_blocked: string }
interface TaskEntry {
  task_id: string; title: string; type: string; status: string; timezone: string | null;
  start: string | null; end: string | null; start_local: string | null;
  eligible: boolean; not_on_calendar_because: string | null;
  calendar_event_id: string | null; calendar_state: string | null; calendar_link: string | null;
  synced_revision: number | null; schedule_revision: number | null; synced_at: string | null;
  error: string | null; conflicts: Array<{ summary?: string; start?: string; end?: string; html_link?: string; unavailable?: boolean; reason?: string }>;
  deep_link: string;
}
interface CalendarEvent {
  id: string; summary: string; start: string | null; end: string | null; status: string;
  html_link: string | null; mine: boolean; azkt_task_id: string | null; all_day: boolean;
}
interface CalendarStatus {
  connection: Connection; connected: boolean; writes_enabled: boolean;
  write_scope_granted: boolean | null; read_scope_granted: boolean | null;
  calendar_id: string; conflict_check: boolean;
  durations: Record<string, number>;
  read_blocked: Blockage | null; write_blocked: Blockage | null;
  rules: { always: string[]; with_a_block: string[]; never: string[] };
  counts: Record<string, number>; problems: TaskEntry[];
  setting_version: number; scopes: { read: string; write: string };
}
interface DayResp {
  from: string; to: string; timezone: string; tasks: TaskEntry[]; conflicts: number;
  calendar: { available: boolean; setup_blocked: string | null; calendar_id?: string; events: CalendarEvent[] };
}

const TYPE_LABEL: Record<string, string> = { call: "Call", meeting: "Meeting", follow_up: "Follow-up", operational: "Shop work" };
const STATE: Record<string, { label: string; tone: "ok" | "wait" | "risk" | "blocked" | "soft"; blurb: string }> = {
  synced: { label: "On the calendar", tone: "ok", blurb: "" },
  cancelled: { label: "Taken off", tone: "soft", blurb: "The task was completed or cancelled, so the entry was removed." },
  skipped: { label: "Not an appointment", tone: "soft", blurb: "" },
  setup_blocked: { label: "Held — setup", tone: "wait", blurb: "Nothing was written. Finish the two steps above and it goes on by itself." },
  failed: { label: "Failed", tone: "blocked", blurb: "The calendar refused the write. It is retried with backoff." },
  unknown: { label: "Result unknown", tone: "risk", blurb: "The calendar may have stored it before the answer was lost. AZKT looks the entry up before writing again — it never guesses." },
};
function stateView(s: string | null) {
  if (!s) return { label: "Not synced yet", tone: "soft" as const, blurb: "" };
  return STATE[s] || { label: s.replace(/_/g, " "), tone: "soft" as const, blurb: "" };
}

function todayISO(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function clock(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function CalendarSection() {
  const { user } = useAuth();
  const { toast } = useToast();
  const { run, busy } = useCommand();
  const manage = can(user, "connections");
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const [day, setDay] = useState(todayISO());
  const [calendarId, setCalendarId] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);

  const q = useQuery<CalendarStatus | null>(
    (signal) => api.get<CalendarStatus | null>("/api/calendar/status", { signal, tolerate: [403, 404] }), [tick]);
  const dayQ = useQuery<DayResp | null>(
    (signal) => api.get<DayResp | null>(`/api/calendar/today?day=${encodeURIComponent(day)}`, { signal, tolerate: [403, 404] }),
    [tick, day]);

  const s = q.data;
  const stuck = useMemo(() => (s?.problems || []).filter((p) => p.calendar_state !== "skipped"), [s]);
  const effectiveCalendarId = calendarId || s?.calendar_id || "primary";

  const connect = async (enableWrite: boolean) => {
    setProblem(null);
    setConnecting(true);
    try {
      const r = await api.post<{ url: string; expected_identity: string }>("/api/calendar/connect", { enable_write: enableWrite });
      window.location.assign(r.url);
    } catch (e) {
      setProblem(describeError(e));
      setConnecting(false);
    }
  };

  const setWrites = async (enabled: boolean) => {
    setProblem(null);
    const r = await run("cal-writes", "/api/calendar/writes",
      { enabled, calendar_id: effectiveCalendarId, expected_version: s?.setting_version ?? null },
      {
        success: enabled
          ? "AZKT will put calls, meetings and scheduled follow-ups on the calendar."
          : "AZKT will stop creating calendar entries. Entries already there are left alone.",
        onError: (m) => setProblem(m),
      });
    if (r?.status === "blocked") setProblem(r.decision.reasons.join(" · ") || "A check is blocking this.");
    reload();
  };

  const resync = async (taskId?: string) => {
    setProblem(null);
    const r = await run("cal-resync", "/api/calendar/resync", taskId ? { task_id: taskId } : {}, {
      success: "Queued. The worker writes to the calendar; this page shows the result.",
      onError: (m) => setProblem(m),
    });
    if (r?.status === "ok") toast({ message: `${(r.data as { queued?: number } | null)?.queued ?? 0} appointment(s) queued.`, tone: "ok" });
    reload();
  };

  if (q.loading) return <GlassPanel clip padded><Loading label="Loading the calendar setup" rows={4} /></GlassPanel>;
  if (q.error) return <GlassPanel clip padded><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;
  if (!s) {
    return (
      <GlassPanel clip padded>
        <EmptyState align="left" title="Not available for your role" body={whyNot("connections")} />
      </GlassPanel>
    );
  }

  const conn = s.connection;
  const step1Done = s.connected && s.write_scope_granted !== false;
  const step2Done = s.writes_enabled && !s.write_blocked;

  return (
    <div className="stack">
      {problem ? <Notice tone="blocked" lead="Not done" role="alert">{problem}</Notice> : null}

      {!step1Done || !step2Done ? (
        <Notice tone="wait" lead="Nothing is on the calendar yet">
          Calendar writes need two separate things: a connected Google account <em>and</em> your explicit
          permission to create events. Until both are set, appointments are recorded in AZKT and reminders
          still go out — the calendar entry is simply held, and every held task says so.
        </Notice>
      ) : null}

      {/* ── step 1: the account ─────────────────────────────────────── */}
      <GlassPanel clip>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">
              <span>1 · The Google account</span>
              {s.connected ? <Chip size="sm" tone="ok">Connected</Chip> : <Chip size="sm" tone="wait">Not connected</Chip>}
            </span>
            <span className="set-row__meta">
              {conn.account_identity
                ? <>Connected as {conn.account_identity}{conn.connected_at ? <> · <When iso={conn.connected_at} format="long" /></> : null}</>
                : "AZKT connects the business calendar the same way it connects Gmail and Drive. It asks for read access first; the event scope is a separate tick."}
            </span>
            {s.connected ? (
              <span className="fs12 t4">
                {s.write_scope_granted === false
                  ? "Read access only — AZKT can see the day and spot clashes, but cannot create anything."
                  : s.write_scope_granted ? "Read and create events." : "Scopes were not recorded for this connection."}
              </span>
            ) : null}
            {conn.failure && "message" in conn.failure && conn.failure.message
              ? <span className="fs12" style={{ color: "var(--blocked)" }}>{conn.failure.message}</span> : null}
          </div>
          <div className="set-row__right">
            {manage ? (
              <>
                <Button size="sm" variant={s.connected ? "soft" : "primary"} loading={connecting} onClick={() => connect(true)}>
                  {s.connected ? "Reconnect with event access" : "Connect calendar"}
                </Button>
                {s.connected ? <Button size="sm" variant="ghost" onClick={() => connect(false)} disabled={connecting}>Read-only</Button> : null}
              </>
            ) : <Badge tone="wait">Owner only</Badge>}
          </div>
        </div>

        {/* ── step 2: the capability ────────────────────────────────── */}
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">
              <span>2 · Put calls and meetings on the calendar</span>
              {s.writes_enabled ? <Chip size="sm" tone="ok">On</Chip> : <Chip size="sm" tone="soft">Off</Chip>}
            </span>
            <span className="set-row__meta">
              A separate permission from connecting the account. With it off, AZKT never creates, changes or
              removes a calendar entry.
            </span>
            {s.write_blocked ? <span className="fs12" style={{ color: "var(--risk)" }}>{s.write_blocked.reason}</span> : null}
          </div>
          <div className="set-row__right">
            <Switch
              checked={s.writes_enabled}
              onChange={(on) => setWrites(on)}
              disabled={!manage || busy("cal-writes")}
              disabledReason={manage ? "Saving…" : whyNot("connections")}
              label="Create events"
            />
          </div>
        </div>

        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Which calendar</span>
            <span className="set-row__meta">
              “primary” is the connected account&apos;s own calendar. Give an address instead to use a shared one.
            </span>
          </div>
          <div className="set-row__right" style={{ gap: 8 }}>
            <Input value={effectiveCalendarId} onChange={(e) => setCalendarId(e.target.value)} disabled={!manage}
              placeholder="primary" aria-label="Calendar the events are written to" />
            <Button size="sm" variant="soft" disabled={!manage || effectiveCalendarId === s.calendar_id}
              disabledReason={!manage ? whyNot("connections") : "This is already the calendar AZKT writes to."}
              loading={busy("cal-writes")} onClick={() => setWrites(s.writes_enabled)}>
              Save
            </Button>
          </div>
        </div>
      </GlassPanel>

      {/* ── the rule, in the owner's own words ───────────────────────── */}
      <GlassPanel padded>
        <div className="stack-sm">
          <div className="eyebrow">What goes on the calendar</div>
          <span className="fs13 t3">
            Appointments only — “meetings, call people back, schedules”. Shop work stays a to-do, and a task with
            no time is never turned into one.
          </span>
          <KeyValues items={[
            ["Always", s.rules.always.map((t) => TYPE_LABEL[t] || t).join(" · ") + " — once the task has a time"],
            ["Only with a scheduled block", s.rules.with_a_block.map((t) => TYPE_LABEL[t] || t).join(" · ") + " — needs a start and an end"],
            ["Never", s.rules.never.map((t) => TYPE_LABEL[t] || t).join(" · ")],
            ["Length when only a start is known", `Call ${s.durations.call} min · Meeting ${s.durations.meeting} min · Follow-up ${s.durations.follow_up} min`],
            ["Clash check", s.conflict_check ? "On — an overlapping entry is recorded on the task and raised with you" : "Off"],
          ]} />
          <span className="set-foot" style={{ padding: 0 }}>
            AZKT keeps sending the reminder itself, so the calendar entry carries no second popup, and nobody is
            ever invited as a guest — writing to a customer is a separate approval.
          </span>
        </div>
      </GlassPanel>

      {/* ── the day ──────────────────────────────────────────────────── */}
      <GlassPanel clip>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Schedule</span>
            <span className="set-row__meta">
              {dayQ.data ? <>Shown in {dayQ.data.timezone}{dayQ.data.conflicts ? ` · ${dayQ.data.conflicts} clash(es)` : ""}</> : "Loading"}
            </span>
          </div>
          <div className="set-row__right">
            <Input type="date" value={day} onChange={(e) => setDay(e.target.value || todayISO())} aria-label="Day" />
            <Button size="sm" variant="ghost" onClick={reload}>Refresh</Button>
          </div>
        </div>
        {dayQ.loading ? <div style={{ padding: 16 }}><Loading rows={2} label="Loading the day" /></div>
          : dayQ.error ? <div style={{ padding: 16 }}><ErrorState error={dayQ.error} onRetry={dayQ.reload} /></div>
            : !dayQ.data ? <div style={{ padding: 16 }}><span className="not-recorded">Not available for your role.</span></div>
              : !dayQ.data.tasks.length ? (
                <div style={{ padding: "0 16px 16px" }}>
                  <EmptyState align="left" title="No appointments that day" body="Calls, meetings and scheduled follow-ups appear here. Shop work lives on the Tasks screen." />
                </div>
              ) : dayQ.data.tasks.map((t) => {
                const view = stateView(t.calendar_state);
                return (
                  <div key={t.task_id} className="set-row">
                    <div className="set-row__main">
                      <span className="set-row__title">
                        <span className="tnum">{clock(t.start)}</span>
                        <span>{t.title}</span>
                        <Chip size="sm" tone="soft">{TYPE_LABEL[t.type] || t.type}</Chip>
                        <Chip size="sm" tone={view.tone}>{view.label}</Chip>
                        {t.conflicts.length ? <Chip size="sm" tone="amber">Clash</Chip> : null}
                      </span>
                      <span className="set-row__meta">
                        {t.start_local || <NotRecorded text="No time recorded" />}
                        {t.synced_at ? <> · written <When iso={t.synced_at} relative /></> : null}
                        {t.schedule_revision !== t.synced_revision && t.calendar_state === "synced"
                          ? " · rescheduled since the last write" : null}
                      </span>
                      {!t.eligible && t.not_on_calendar_because
                        ? <span className="fs12 t4">Not on the calendar: {t.not_on_calendar_because}.</span> : null}
                      {t.error ? <span className="fs12" style={{ color: "var(--blocked)" }}>{t.error}</span> : null}
                      {t.conflicts.map((c, i) => (
                        <span key={i} className="fs12" style={{ color: "var(--risk)" }}>
                          {c.unavailable
                            ? `The clash check could not run: ${c.reason || "the calendar was unreachable"}.`
                            : `Overlaps “${c.summary || "an existing entry"}”${c.start ? ` at ${clock(c.start)}` : ""}.`}
                        </span>
                      ))}
                    </div>
                    <div className="set-row__right">
                      {t.calendar_link ? <a className="fs13" href={t.calendar_link} target="_blank" rel="noopener noreferrer">Open in Google</a> : null}
                      <Link className="fs13" to={`/tasks/${encodeURIComponent(t.task_id)}`}>Task</Link>
                      {manage && t.eligible ? (
                        <Button size="sm" variant="ghost" loading={busy("cal-resync")} onClick={() => resync(t.task_id)}>Re-sync</Button>
                      ) : null}
                    </div>
                  </div>
                );
              })}
        {dayQ.data && !dayQ.data.calendar.available ? (
          <div className="set-foot">
            {dayQ.data.calendar.setup_blocked || "The calendar itself was not read, so only AZKT's own records are shown."}
          </div>
        ) : dayQ.data ? (
          <Expander title={`Everything on ${dayQ.data.calendar.calendar_id || "the calendar"} that day · ${dayQ.data.calendar.events.length}`}>
            <div className="stack-sm" style={{ paddingTop: 6 }}>
              {dayQ.data.calendar.events.length
                ? dayQ.data.calendar.events.map((e) => (
                  <span key={e.id} className="fs13">
                    <span className="tnum">{e.all_day ? "All day" : `${clock(e.start)}–${clock(e.end)}`}</span>{" "}
                    {e.summary || "(no title)"}{e.mine ? <span className="t4"> · from AZKT</span> : null}
                  </span>
                ))
                : <span className="not-recorded">Nothing on the calendar that day.</span>}
            </div>
          </Expander>
        ) : null}
      </GlassPanel>

      {/* ── anything the sync could not finish ───────────────────────── */}
      <GlassPanel clip>
        <div style={{ padding: "14px 16px 6px" }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>Appointments the calendar does not have</h3>
        </div>
        {!stuck.length ? (
          <div style={{ padding: "0 16px 16px" }}>
            <EmptyState align="left" title="Nothing waiting"
              body={s.writes_enabled ? "Every appointment is on the calendar." : "With event creation off, nothing is written — that is the setting, not a failure."} />
          </div>
        ) : stuck.map((t) => {
          const view = stateView(t.calendar_state);
          return (
            <div key={t.task_id} className="set-row">
              <div className="set-row__main">
                <span className="set-row__title">
                  <span>{t.title}</span>
                  <Chip size="sm" tone={view.tone}>{view.label}</Chip>
                </span>
                <span className="set-row__meta">{t.start_local || <NotRecorded text="No time recorded" />}</span>
                {view.blurb ? <span className="fs12 t4">{view.blurb}</span> : null}
                {t.error ? <span className="fs12" style={{ color: "var(--blocked)" }}>{t.error}</span> : null}
              </div>
              <div className="set-row__right">
                <Link className="fs13" to={`/tasks/${encodeURIComponent(t.task_id)}`}>Task</Link>
                {manage ? <Button size="sm" variant="soft" loading={busy("cal-resync")} onClick={() => resync(t.task_id)}>Try again</Button> : null}
              </div>
            </div>
          );
        })}
        {manage ? (
          <div className="set-foot">
            <Button size="sm" variant="ghost" loading={busy("cal-resync")} disabled={!s.writes_enabled}
              disabledReason="Turn on event creation first — there is nothing to re-sync to." onClick={() => resync()}>
              Re-sync every appointment
            </Button>
            <span className="fs12 t4" style={{ marginLeft: 8 }}>
              Queues durable work for the worker; nothing is written from this page.
            </span>
          </div>
        ) : null}
      </GlassPanel>

      <Expander title="Details">
        <KeyValues items={[
          ["Calendar", s.calendar_id],
          ["Read scope", s.scopes.read],
          ["Write scope", s.scopes.write],
          ["Read access", s.read_blocked ? s.read_blocked.reason : "Available"],
          ["Write access", s.write_blocked ? s.write_blocked.reason : "Available"],
          ["Appointments by state", Object.entries(s.counts).map(([k, n]) => `${stateView(k === "none" ? null : k).label}: ${n}`).join(" · ") || <NotRecorded />],
          ["Last successful call", s.connection.last_success_at ? <When iso={s.connection.last_success_at} format="long" /> : <NotRecorded text="Never" />],
        ]} />
      </Expander>
    </div>
  );
}
