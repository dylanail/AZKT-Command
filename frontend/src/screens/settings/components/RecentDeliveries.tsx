/* Settings › Reminders › "Recent reminder deliveries".
   There is no account-wide delivery feed: GET /api/notifications/deliveries takes one task_id
   (backend/app/routers/notifications.py), so this reads the person's own recent tasks and asks for
   each one's deliveries. Every state word comes from the server's `state_label` — nothing is guessed,
   and a Telegram send that failed over to email is visible instead of silent. */
import { api } from "../../../lib/api";
import { useQuery } from "../../../lib/useQuery";
import { Chip, ErrorState, GlassPanel, Loading, When } from "../../../ui";
import type { TaskView } from "../../tasks/types";
import { CHANNEL_LABELS, DELIVERY_KIND_LABELS, deliveryTone, type Delivery, type DeliveriesResp } from "./notifyTypes";

const TASKS_SCANNED = 8;
const ROWS_SHOWN = 12;

interface Row extends Delivery { taskTitle: string }
interface Loaded { rows: Row[]; scanned: number; note: string; denied: boolean }

async function load(signal: AbortSignal): Promise<Loaded> {
  const list = await api.get<{ items?: TaskView[] } | null>("/api/tasks?view=my&include_closed=true&limit=40", { signal, tolerate: [403, 404] });
  if (list === null) return { rows: [], scanned: 0, note: "", denied: true };
  const tasks = (list.items || [])
    .filter((t) => t.due_at || t.reminder_kind)
    .sort((a, b) => String(b.due_at || "").localeCompare(String(a.due_at || "")))
    .slice(0, TASKS_SCANNED);
  const titles = new Map(tasks.map((t) => [t.id, t.title]));
  const results = await Promise.all(tasks.map((t) =>
    api.get<DeliveriesResp | null>(`/api/notifications/deliveries?task_id=${encodeURIComponent(t.id)}`, { signal, tolerate: [403, 404] })
      .catch(() => null)));
  const rows: Row[] = [];
  let note = "";
  for (const r of results) {
    if (!r) continue;
    if (r.note) note = r.note;
    for (const d of r.deliveries || []) rows.push({ ...d, taskTitle: titles.get(r.task_id) || "Task" });
  }
  const at = (d: Delivery) => d.delivered_at || d.sent_at || d.deliver_at || "";
  rows.sort((a, b) => String(at(b)).localeCompare(String(at(a))));
  return { rows: rows.slice(0, ROWS_SHOWN), scanned: tasks.length, note, denied: false };
}

/** "Fallback after telegram failed" — only said when the earlier attempt is in the same list. */
function fallbackNote(d: Row, byId: Map<string, Row>): string | null {
  if (!d.fallback_of_id) return null;
  const parent = byId.get(d.fallback_of_id);
  if (!parent) return "Sent because an earlier attempt on the other channel did not go through.";
  const channel = CHANNEL_LABELS[parent.channel] || parent.channel;
  return `Sent because the ${channel} attempt is "${parent.state_label}".`;
}

export function RecentDeliveries() {
  const q = useQuery<Loaded>((signal) => load(signal), []);

  if (q.loading) return <GlassPanel clip padded><Loading label="Loading recent reminder deliveries" rows={2} /></GlassPanel>;
  if (q.error) return <GlassPanel clip padded><ErrorState error={q.error} onRetry={q.reload} title="Couldn't load recent deliveries" /></GlassPanel>;

  const data = q.data;
  const byId = new Map((data?.rows || []).map((r) => [r.id, r]));

  return (
    <div className="stack-sm">
      <div className="eyebrow">Recent reminder deliveries</div>
      <GlassPanel clip padded>
        {data?.denied ? (
          <span className="not-recorded">Your role can&apos;t list tasks, so reminder deliveries can&apos;t be shown here.</span>
        ) : !data || !data.rows.length ? (
          <span className="not-recorded">
            {data && data.scanned === 0
              ? "None of your recent tasks has a reminder, so nothing has been scheduled to send."
              : "Nothing has been scheduled or sent for your recent tasks yet."}
          </span>
        ) : (
          <div>
            {data.rows.map((d) => {
              const why = fallbackNote(d, byId);
              const when = d.delivered_at || d.sent_at || d.deliver_at;
              return (
                <div key={d.id} className="nf-del">
                  <span className="nf-del__task">{d.taskTitle}</span>
                  <Chip size="sm" tone={deliveryTone(d.state)}>{d.state_label || d.state}</Chip>
                  <span className="nf-del__meta">
                    {DELIVERY_KIND_LABELS[d.kind] || d.kind} · {CHANNEL_LABELS[d.channel] || d.channel}
                    {" · "}
                    {when ? <When iso={when} format="long" /> : "Time not recorded"}
                    {d.late ? " · late" : ""}
                    {d.attempts > 1 ? ` · ${d.attempts} attempts` : ""}
                    {d.destination_hidden ? " · sent to someone else; the address stays private" : ""}
                  </span>
                  {why ? <span className="nf-del__why">{why}</span> : null}
                  {d.last_error ? <span className="nf-del__why">{d.last_error}</span> : null}
                  {d.cancel_reason ? <span className="nf-del__why">Cancelled: {d.cancel_reason}</span> : null}
                </div>
              );
            })}
          </div>
        )}
      </GlassPanel>
      <span className="fs12 t4">
        {data?.note || "Provider acceptance is not proof the message was seen."}
        {data && !data.denied && data.scanned ? ` Covers your ${data.scanned} most recent scheduled ${data.scanned === 1 ? "task" : "tasks"}.` : ""}
      </span>
    </div>
  );
}
