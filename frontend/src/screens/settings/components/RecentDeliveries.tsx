/* Settings › Reminders › "Recent reminder deliveries".
   One request: GET /api/notifications/deliveries?mine=true (backend/app/routers/notifications.py →
   reminders.deliveries_for_user), which lists the reminders addressed to you, newest first, whatever
   record they came from. Every state word comes from the server's `state_label` — nothing is guessed,
   and a Telegram send that failed over to email is visible instead of silent. */
import { Link } from "react-router-dom";
import { api } from "../../../lib/api";
import { entityHref, entityLabel, shortId } from "../../../lib/links";
import { useQuery } from "../../../lib/useQuery";
import { Chip, ErrorState, GlassPanel, Loading, When } from "../../../ui";
import { CHANNEL_LABELS, DELIVERY_KIND_LABELS, deliveryTone, type Delivery, type MyDeliveriesResp } from "./notifyTypes";

const LIMIT = 20;

/** "Fallback after telegram failed" — only said when the earlier attempt is in the same list. */
function fallbackNote(d: Delivery, byId: Map<string, Delivery>): string | null {
  if (!d.fallback_of_id) return null;
  const parent = byId.get(d.fallback_of_id);
  if (!parent) return "Sent because an earlier attempt on the other channel did not go through.";
  const channel = CHANNEL_LABELS[parent.channel] || parent.channel;
  return `Sent because the ${channel} attempt is "${parent.state_label}".`;
}

/** The record this reminder was about, when the row names one. */
function RecordLink({ d }: { d: Delivery }) {
  if (d.task_id) return <Link to={`/tasks/${encodeURIComponent(d.task_id)}`}>Open the task</Link>;
  const href = entityHref(d.entity_kind, d.entity_id);
  if (!href) return null;
  return <Link to={href}>Open the {entityLabel(d.entity_kind)} {shortId(d.entity_id)}</Link>;
}

export function RecentDeliveries() {
  const q = useQuery<MyDeliveriesResp | null>(
    (signal) => api.get<MyDeliveriesResp | null>(`/api/notifications/deliveries?mine=true&limit=${LIMIT}`, { signal, tolerate: [403, 404] }),
    [],
  );

  if (q.loading) return <GlassPanel clip padded><Loading label="Loading recent reminder deliveries" rows={2} /></GlassPanel>;
  if (q.error) return <GlassPanel clip padded><ErrorState error={q.error} onRetry={q.reload} title="Couldn't load recent deliveries" /></GlassPanel>;

  const data = q.data;
  const rows = data?.deliveries || [];
  const byId = new Map(rows.map((r) => [r.id, r]));

  return (
    <div className="stack-sm">
      <div className="eyebrow">Recent reminder deliveries</div>
      <GlassPanel clip padded>
        {data === null ? (
          <span className="not-recorded">Your account can&apos;t list its own reminder deliveries, so nothing is shown here.</span>
        ) : !rows.length ? (
          <span className="not-recorded">Nothing has been scheduled or sent to you yet.</span>
        ) : (
          <div>
            {rows.map((d) => {
              const why = fallbackNote(d, byId);
              const when = d.delivered_at || d.sent_at || d.deliver_at;
              return (
                <div key={d.id} className="nf-del">
                  <span className="nf-del__task">{DELIVERY_KIND_LABELS[d.kind] || d.kind}</span>
                  <Chip size="sm" tone={deliveryTone(d.state)}>{d.state_label || d.state}</Chip>
                  <span className="nf-del__meta">
                    {CHANNEL_LABELS[d.channel] || d.channel}
                    {" · "}
                    {when ? <When iso={when} format="long" /> : "Time not recorded"}
                    {d.late ? " · late" : ""}
                    {d.attempts > 1 ? ` · ${d.attempts} attempts` : ""}
                    {d.destination_hidden ? " · sent to someone else; the address stays private" : ""}
                    {d.task_id || entityHref(d.entity_kind, d.entity_id) ? <> · <RecordLink d={d} /></> : null}
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
        {data && data.total > rows.length ? ` Showing the ${rows.length} most recent of ${data.total}.` : ""}
      </span>
    </div>
  );
}
