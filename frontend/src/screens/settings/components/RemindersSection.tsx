/* Settings › Reminders. Your own preferences: PATCH /api/me/prefs (channel and in-app bell per reminder
   kind, quiet hours, digest time, reminder email — "unverified" until a verification flow confirms it —
   and timezone). Those are the preference keys the server accepts (team.NotificationPrefsIn); anything
   else on this page is shown read-only with where it is actually set.
   Owner defaults for everyone: GET/POST /api/settings/reminders (versioned).
   Business-wide reminder timing: GET /api/notifications/prefs → business_defaults. */
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, describeError, type CommandResult } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useQuery } from "../../../lib/useQuery";
import { useCommand } from "../../../lib/useCommand";
import { can } from "../../../lib/perms";
import { Badge, Button, ErrorState, Field, GlassPanel, Input, Loading, Notice, Select, Switch, When, useToast } from "../../../ui";
import { CHANNEL_MODES, REMINDER_KINDS, type MeResp, type RemindersSettings, type SettingEntry } from "./types";
import { RecentDeliveries } from "./RecentDeliveries";
import type { NotificationPrefsResp } from "./notifyTypes";
import "../../../styles/notify.css";

const TZS = ["America/Phoenix", "America/Los_Angeles", "America/Denver", "America/Chicago", "America/New_York", "Asia/Tokyo", "UTC"];
const KINDS = REMINDER_KINDS.map((k) => k.key);

/* `inapp` is a single switch for everything or one per reminder kind; a kind the map doesn't mention is
   on (reminders.inapp_enabled). The screen keeps one switch per kind and folds them back on save. */
type InappPref = boolean | Record<string, boolean> | undefined;
function readInapp(v: InappPref): Record<string, boolean> {
  if (v === false) return Object.fromEntries(KINDS.map((k) => [k, false]));
  if (v && typeof v === "object") return Object.fromEntries(KINDS.map((k) => [k, v[k] !== false]));
  return Object.fromEntries(KINDS.map((k) => [k, true]));
}
function writeInapp(m: Record<string, boolean>): boolean | Record<string, boolean> {
  const on = KINDS.map((k) => m[k] !== false);
  if (on.every(Boolean)) return true;
  if (!on.some(Boolean)) return false;
  return Object.fromEntries(KINDS.map((k) => [k, m[k] !== false]));
}
const OFFSETS = [{ value: "at", label: "At the time" }, { value: "15m", label: "15 minutes before" }, { value: "1h", label: "1 hour before" }, { value: "1d", label: "1 day before" }, { value: "custom", label: "Custom per task" }];

/* The settings below are not per-person preferences — team.NotificationPrefsIn accepts only channels,
   the in-app bell, quiet hours and the digest time — so they are shown read-only with where they are set. */
function BusinessTiming({ biz }: { biz: NotificationPrefsResp["business_defaults"] | null }) {
  return (
    <GlassPanel clip>
      <div className="set-row" style={{ borderBottom: "1px solid var(--line2)" }}>
        <div className="set-row__main">
          <span className="eyebrow">Set in configuration, not here</span>
          <span className="set-row__meta">These apply to everyone. Ask the owner to change them.</span>
        </div>
      </div>
      <div className="set-row">
        <div className="set-row__main">
          <span className="set-row__title">How early a task reminder arrives</span>
          <span className="set-row__meta">Chosen on each task: at the time, 15 minutes, 1 hour, 1 day, or a custom number of minutes. The owner sets which one new tasks start with.</span>
        </div>
        <div className="set-row__right"><Link to="/tasks" className="fs13">Open tasks</Link></div>
      </div>
      {biz ? (
        <>
          <div className="set-row">
            <div className="set-row__main">
              <span className="set-row__title">Owner reminder address</span>
              <span className="set-row__meta">
                {biz.owner_reminder_email_configured
                  ? "Where a reminder goes when the person it is for has no email of their own."
                  : "Not set on the server. A reminder for someone with no email has nowhere to go."}
              </span>
            </div>
            <div className="set-row__right">
              {biz.owner_reminder_email_configured
                ? <span className="fs13 t3" style={{ overflowWrap: "anywhere" }}>{biz.owner_reminder_email || biz.owner_reminder_email_masked || "Configured"}</span>
                : <Badge tone="risk">Not configured</Badge>}
            </div>
          </div>
          <div className="set-row">
            <div className="set-row__main">
              <span className="set-row__title">Overdue reminder</span>
              <span className="set-row__meta">Sent once, {biz.overdue_delay_minutes} minutes after a task&apos;s time passes, if it is still open.</span>
            </div>
            <div className="set-row__right"><span className="tnum t3 fs13">{biz.overdue_delay_minutes} min</span></div>
          </div>
          <div className="set-row">
            <div className="set-row__main">
              <span className="set-row__title">Counted as late</span>
              <span className="set-row__meta">A reminder sent more than {biz.late_grace_minutes} minutes after its time is marked late instead of quietly passing.</span>
            </div>
            <div className="set-row__right"><span className="tnum t3 fs13">{biz.late_grace_minutes} min</span></div>
          </div>
          <div className="set-row">
            <div className="set-row__main">
              <span className="set-row__title">Too old to send</span>
              <span className="set-row__meta">After {biz.obsolete_after_hours} hours a missed reminder is collapsed into the morning digest rather than arriving out of time.</span>
            </div>
            <div className="set-row__right"><span className="tnum t3 fs13">{biz.obsolete_after_hours} h</span></div>
          </div>
          <div className="set-row">
            <div className="set-row__main">
              <span className="set-row__title">Business digest time</span>
              <span className="set-row__meta">Your own digest time above overrides this for you.</span>
            </div>
            <div className="set-row__right"><span className="tnum t3 fs13">{biz.digest_local_time}</span></div>
          </div>
        </>
      ) : (
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Reminder timing</span>
            <span className="set-row__meta">The server didn&apos;t return the business timing settings, so AZKT won&apos;t guess them here.</span>
          </div>
        </div>
      )}
    </GlassPanel>
  );
}

function MyPrefs() {
  const { user, refresh } = useAuth();
  const { toast } = useToast();
  const me = useQuery<MeResp>((signal) => api.get<MeResp>("/api/me", { signal }), []);
  const prefsQ = useQuery<NotificationPrefsResp | null>((signal) => api.get<NotificationPrefsResp | null>("/api/notifications/prefs", { signal, tolerate: [403, 404, 501] }), []);
  const [channels, setChannels] = useState<Record<string, string>>({});
  const [inapp, setInapp] = useState<Record<string, boolean>>(() => readInapp(undefined));
  const [quiet, setQuiet] = useState(false);
  const [qStart, setQStart] = useState("21:00");
  const [qEnd, setQEnd] = useState("07:00");
  const [digest, setDigest] = useState("08:00");
  const [email, setEmail] = useState("");
  const [tz, setTz] = useState("America/Phoenix");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const p = me.data?.prefs;
    if (!p) return;
    setChannels({ ...p.notification_prefs.channels });
    setInapp(readInapp(p.notification_prefs.inapp));
    setQuiet(!!p.notification_prefs.quiet_hours);
    setQStart(p.notification_prefs.quiet_hours?.start || "21:00");
    setQEnd(p.notification_prefs.quiet_hours?.end || "07:00");
    setDigest(p.notification_prefs.digest_time || "08:00");
    setEmail(p.reminder_email || "");
    setTz(p.timezone || "America/Phoenix");
  }, [me.data]);

  const p = me.data?.prefs;
  const telegram = me.data?.pairing?.telegram;
  const paired = telegram?.status === "active";
  const dirty = useMemo(() => {
    if (!p) return false;
    const np = p.notification_prefs;
    if (JSON.stringify(channels) !== JSON.stringify(np.channels)) return true;
    if (JSON.stringify(writeInapp(inapp)) !== JSON.stringify(writeInapp(readInapp(np.inapp)))) return true;
    if (quiet !== !!np.quiet_hours) return true;
    if (quiet && (qStart !== np.quiet_hours?.start || qEnd !== np.quiet_hours?.end)) return true;
    if (digest !== (np.digest_time || "08:00")) return true;
    if (email.trim().toLowerCase() !== (p.reminder_email || "")) return true;
    if (tz !== (p.timezone || "America/Phoenix")) return true;
    return false;
  }, [p, channels, inapp, quiet, qStart, qEnd, digest, email, tz]);

  const save = async () => {
    if (!p) return;
    const np = p.notification_prefs;
    const body: Record<string, unknown> = {};
    const prefs: Record<string, unknown> = {};
    const changedChannels = Object.fromEntries(Object.entries(channels).filter(([k, v]) => np.channels[k] !== v));
    if (Object.keys(changedChannels).length) prefs.channels = changedChannels;
    const nextInapp = writeInapp(inapp);
    if (JSON.stringify(nextInapp) !== JSON.stringify(writeInapp(readInapp(np.inapp)))) prefs.inapp = nextInapp;
    if (quiet && (!np.quiet_hours || qStart !== np.quiet_hours.start || qEnd !== np.quiet_hours.end)) prefs.quiet_hours = { start: qStart, end: qEnd };
    if (!quiet && np.quiet_hours) prefs.clear_quiet_hours = true;
    if (digest !== (np.digest_time || "08:00")) prefs.digest_time = digest;
    if (Object.keys(prefs).length) body.notification_prefs = prefs;
    if (email.trim().toLowerCase() !== (p.reminder_email || "")) body.reminder_email = email.trim();
    if (tz !== (p.timezone || "America/Phoenix")) body.timezone = tz;
    if (!Object.keys(body).length) return;
    setSaving(true);
    try {
      const r = await api.patch<Partial<CommandResult>>("/api/me/prefs", body);
      if (r && r.status === "blocked") toast({ title: "Blocked", message: (r.decision?.reasons || []).join(" · ") || "Not saved.", tone: "blocked" });
      else toast({ message: body.reminder_email ? "Saved. Your reminder email is unverified until you confirm it." : "Preferences saved.", tone: "ok" });
      me.reload();
      void refresh();
    } catch (e) {
      toast({ message: describeError(e), tone: "blocked", duration: 6000 });
    } finally {
      setSaving(false);
    }
  };

  const allInapp = KINDS.every((k) => inapp[k] !== false);
  const anyInapp = KINDS.some((k) => inapp[k] !== false);
  const setAllInapp = (on: boolean) => setInapp(Object.fromEntries(KINDS.map((k) => [k, on])));

  if (me.loading) return <GlassPanel clip><Loading label="Loading your preferences" rows={3} /></GlassPanel>;
  if (me.error) return <GlassPanel clip><ErrorState error={me.error} onRetry={me.reload} /></GlassPanel>;
  if (!p) return null;

  return (
    <div className="stack">
      {!paired ? (
        <Notice tone="wait" lead="Telegram not paired" action={<Button size="sm" variant="soft" to="/settings/telegram">Open Telegram</Button>}>
          {telegram?.note || "Telegram choices fall back to email until your Telegram is paired through the bot."}
        </Notice>
      ) : telegram?.delivery_failures ? (
        <Notice tone="risk" lead="Telegram delivery failing" action={<Button size="sm" variant="soft" to="/settings/telegram">Open Telegram</Button>}>
          {telegram.delivery_failures} recent {telegram.delivery_failures === 1 ? "failure" : "failures"}; email is used meanwhile.
        </Notice>
      ) : null}
      <GlassPanel clip>
        <div className="set-row" style={{ borderBottom: "1px solid var(--line2)" }}>
          <div className="set-row__main">
            <span className="eyebrow">Where each reminder reaches you</span>
            <span className="set-row__meta">The switch is the bell inside AZKT; the menu is what leaves AZKT.</span>
          </div>
          <div className="set-row__right">
            <Switch checked={allInapp} onChange={setAllInapp} label="Bell for everything" style={{ width: "auto" }} />
          </div>
        </div>
        {REMINDER_KINDS.map((k) => (
          <div key={k.key} className="set-row">
            <div className="set-row__main"><span className="set-row__title">{k.label}</span><span className="set-row__meta">{k.hint}</span></div>
            <div className="set-row__right">
              <Switch checked={inapp[k.key] !== false} onChange={(on) => setInapp((c) => ({ ...c, [k.key]: on }))}
                label={<span className="sr-only">Show {k.label.toLowerCase()} in the bell</span>} style={{ width: "auto" }} />
              <Select aria-label={`${k.label} channel`} value={channels[k.key] || "email_only"} onChange={(e) => setChannels((c) => ({ ...c, [k.key]: e.target.value }))}>
                {CHANNEL_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </Select>
            </div>
          </div>
        ))}
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__meta">
              {allInapp
                ? "Everything AZKT files for you shows in the bell."
                : anyInapp
                  ? "The kinds switched off above are not filed to the bell. They still go to the channel you chose."
                  : "Nothing new is filed to the bell. Tasks and approvals still appear there — they are read straight from the records, not filed."}
            </span>
          </div>
        </div>
      </GlassPanel>
      <GlassPanel clip>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Quiet hours</span><span className="set-row__meta">Nothing but blockers between these times, in your timezone.</span></div>
          <div className="set-row__right">
            {quiet ? <><Input type="time" aria-label="Quiet from" value={qStart} onChange={(e) => setQStart(e.target.value)} style={{ minWidth: 0, width: 120 }} /><span className="t3">to</span><Input type="time" aria-label="Quiet until" value={qEnd} onChange={(e) => setQEnd(e.target.value)} style={{ minWidth: 0, width: 120 }} /></> : null}
            <Switch checked={quiet} onChange={setQuiet} label={<span className="sr-only">Quiet hours</span>} style={{ width: "auto", minHeight: 40 }} />
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Morning digest time</span><span className="set-row__meta">Only on days with something due.</span></div>
          <div className="set-row__right"><Input type="time" aria-label="Digest time" value={digest} onChange={(e) => setDigest(e.target.value)} style={{ minWidth: 0, width: 120 }} /></div>
        </div>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Reminder email {p.reminder_email && !p.reminder_email_verified ? <Badge tone="risk">unverified</Badge> : p.reminder_email_verified_at ? <Badge tone="ok">verified</Badge> : null}</span>
            <span className="set-row__meta">
              {p.reminder_email_verified_at ? <>Verified <When iso={p.reminder_email_verified_at} format="date" /></> : p.reminder_email ? "Reminders go here once it's verified; until then, your sign-in email is used." : "Leave blank to use your sign-in email."}
              {!p.reminder_email && !me.data?.email && user?.role === "owner"
                ? " Your account has no email either, so reminders go to the owner address configured on the server, shown lower down this page."
                : ""}
            </span>
          </div>
          <div className="set-row__right"><Input type="email" aria-label="Reminder email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder={me.data?.email || "name@…"} style={{ minWidth: 220 }} /></div>
        </div>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Timezone</span><span className="set-row__meta">Times in AZKT show in Phoenix; reminders use this.</span></div>
          <div className="set-row__right">
            <Input aria-label="Timezone" list="azkt-tz" value={tz} onChange={(e) => setTz(e.target.value)} style={{ minWidth: 200 }} />
            <datalist id="azkt-tz">{TZS.map((z) => <option key={z} value={z} />)}</datalist>
          </div>
        </div>
      </GlassPanel>
      <div className="row-wrap">
        <Button variant="primary" loading={saving} disabled={!dirty} disabledReason="Nothing changed yet." onClick={save}>Save my preferences</Button>
        {dirty ? <span className="fs13 t3">Unsaved changes.</span> : null}
      </div>
      <BusinessTiming biz={prefsQ.data?.business_defaults || null} />
    </div>
  );
}

function OwnerDefaults() {
  const { run, busy } = useCommand();
  const q = useQuery<SettingEntry<RemindersSettings> | null>((signal) => api.get<SettingEntry<RemindersSettings> | null>("/api/settings/reminders", { signal, tolerate: [403, 404] }), []);
  const [v, setV] = useState<RemindersSettings | null>(null);
  useEffect(() => { if (q.data) setV(JSON.parse(JSON.stringify(q.data.value)) as RemindersSettings); }, [q.data]);
  if (q.loading) return <GlassPanel clip><Loading label="Loading defaults" rows={2} /></GlassPanel>;
  if (q.error) return <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;
  if (!q.data || !v) return null;
  const dirty = JSON.stringify(v) !== JSON.stringify(q.data.value);
  const patch = <K extends keyof RemindersSettings>(k: K, part: Partial<RemindersSettings[K]>) => setV((s) => (s ? { ...s, [k]: { ...(s[k] as object), ...part } } : s));
  const save = async () => {
    const r = await run("reminders", "/api/settings/reminders", { value: v, expected_version: q.data?.version, replace: false }, { success: "Defaults saved for everyone.", onError: (_m, e) => { if (e instanceof ApiError && e.code === "conflict") q.reload(); } });
    if (r?.status === "ok") q.reload();
  };
  return (
    <div className="stack">
      <div className="eyebrow">Defaults for everyone <span style={{ textTransform: "none", letterSpacing: 0 }}>· owner · v{q.data.version}{q.data.updated_at ? <> · changed <When iso={q.data.updated_at} relative /></> : " · never changed"}</span></div>
      <GlassPanel clip>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Task reminders</span><span className="set-row__meta">Sent at the offset picked on each task; this is the starting offset.</span></div>
          <div className="set-row__right">
            <Select aria-label="Default offset" value={v.task_reminder.default_offset} onChange={(e) => patch("task_reminder", { default_offset: e.target.value })}>{OFFSETS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</Select>
            <Switch checked={v.task_reminder.enabled} onChange={(on) => patch("task_reminder", { enabled: on })} label={<span className="sr-only">Task reminders on</span>} style={{ width: "auto" }} />
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Overdue</span><span className="set-row__meta">One reminder after a task's time passes.</span></div>
          <div className="set-row__right">
            <Input type="number" min={1} max={1440} aria-label="Minutes after" value={v.overdue.delay_minutes} onChange={(e) => patch("overdue", { delay_minutes: Math.max(1, Number(e.target.value) || 60) })} style={{ width: 96, minWidth: 0 }} /><span className="t3 fs13">min after</span>
            <Switch checked={v.overdue.enabled} onChange={(on) => patch("overdue", { enabled: on })} label={<span className="sr-only">Overdue on</span>} style={{ width: "auto" }} />
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Morning digest</span><span className="set-row__meta">{v.digest.timezone} · {v.digest.non_empty_only ? "only on days with something due" : "every day"}</span></div>
          <div className="set-row__right">
            <Input type="time" aria-label="Digest time" value={v.digest.local_time} onChange={(e) => patch("digest", { local_time: e.target.value })} style={{ width: 120, minWidth: 0 }} />
            <Switch checked={v.digest.non_empty_only} onChange={(on) => patch("digest", { non_empty_only: on })} label="Skip empty days" style={{ width: "auto" }} />
            <Switch checked={v.digest.enabled} onChange={(on) => patch("digest", { enabled: on })} label={<span className="sr-only">Digest on</span>} style={{ width: "auto" }} />
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Deposit paid</span><span className="set-row__meta">Instant confirmation with receipt · {v.deposit_confirmed.owner_only ? "owner only" : "everyone on the deal"}</span></div>
          <div className="set-row__right"><Switch checked={v.deposit_confirmed.enabled} onChange={(on) => patch("deposit_confirmed", { enabled: on })} label={<span className="sr-only">Deposit paid on</span>} style={{ width: "auto" }} /></div>
        </div>
        <div className="set-row">
          <div className="set-row__main"><span className="set-row__title">Reminders to employees</span><span className="set-row__meta">Off: only the owner and managers get reminders. Customer sends are a separate permission.</span></div>
          <div className="set-row__right"><Switch checked={v.employee_reminders_enabled} onChange={(on) => setV((s) => (s ? { ...s, employee_reminders_enabled: on } : s))} label={<span className="sr-only">Employee reminders</span>} style={{ width: "auto" }} /></div>
        </div>
        {REMINDER_KINDS.slice(0, 4).map((k) => (
          <div key={k.key} className="set-row">
            <div className="set-row__main"><span className="set-row__title">Default channel · {k.label}</span><span className="set-row__meta">Each person can override this for themselves.</span></div>
            <div className="set-row__right">
              <Select aria-label={`Default channel for ${k.label}`} value={v.channels[k.key] || "email_only"} onChange={(e) => setV((s) => (s ? { ...s, channels: { ...s.channels, [k.key]: e.target.value } } : s))}>
                {CHANNEL_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </Select>
            </div>
          </div>
        ))}
      </GlassPanel>
      <div className="row-wrap">
        <Button variant="primary" loading={busy("reminders")} disabled={!dirty} disabledReason="Nothing changed yet." onClick={save}>Save defaults</Button>
        <Button variant="ghost" disabled={!dirty} disabledReason="Nothing to discard." onClick={() => setV(JSON.parse(JSON.stringify(q.data?.value)) as RemindersSettings)}>Discard</Button>
      </div>
    </div>
  );
}

export function RemindersSection() {
  const { user } = useAuth();
  return (
    <div className="stack-lg">
      <MyPrefs />
      <RecentDeliveries />
      {can(user, "settings") ? <OwnerDefaults /> : null}
    </div>
  );
}
