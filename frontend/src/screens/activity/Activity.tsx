/* Activity: append-only log; receipts, run ids and policy versions inside expanders.
   TODO(screen builder): GET /api/activity?since=&kind=&entity= (time, actor, what, entity, kind, state, receipt, run, policy, sources). */
import { Scaffold } from "../scaffold";

export default function Activity() {
  return (
    <Scaffold title="Activity" subtitle="Everything that happened, who did it, and the receipt." probe="/api/activity" emptyTitle="Nothing recorded yet" emptyBody="Every automated action writes one row here with a receipt." />
  );
}
