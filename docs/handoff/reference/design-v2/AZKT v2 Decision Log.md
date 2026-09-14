# AZKT v2 — decision log (direction set, Sep 12 2026)

## Resolved in this pass
- Light default, dark toggle persisted. Brief §Review basis.
- Six direct destinations (Home, Vehicles, Import requests, Tasks, Inbox, Contacts) + Activity, Finance, Settings below a divider. "Work" wrapper removed.
- Ask AZKT closed by default; opens in the one right-hand inspector, shared with fact Source details.
- Home order: status sentence → Needs your decision (concise rows, Review opens full approval) → Needs attention → In progress → Completed (collapsed).
- Vehicles: list default; named views All / Sourcing / Shipping / Shop / Sales; Shop has a Board toggle with 4 fixed columns and a Move stage button.
- Vehicle detail: identity + one health label + one primary action in the header; five sections Overview · Work · Files · Sale · Money (owner).
- Approval surface: exact payload, recipient, attachment, scope, checks, expandable sources/technical details; Approve & send → Awaiting execution → Confirmed (receipt). Edit creates a new version and invalidates the old one ("Details changed — review again").
- Inbox reply: unresolved check shown first, passed checks summarised, Review & send disabled with reason until resolved; Take over pauses automation visibly; Resume revalidates.
- Employee mobile: My tasks → task → Complete task / I'm blocked → evidence sheet → Saved (Awaiting verification). Upload failure keeps a draft ("Waiting to upload"); task is not complete.

## Open for Dylan (not fabricated)
- Deposit amounts and the Active-search gate evidence.
- Which operational stage gates may be overridden with reason (policy matrix). Mandatory buyer requirements are never overridable.
- Mandatory-Unknown bidding rule: prototyped as "Needs confirmation" blocking bid readiness; needs owner confirmation.
- Connection freshness thresholds (when "stale" becomes "expired").
- Accounting authority: what Finance may write back vs. export only.
- Bulk publishing approval scope (kept per payload for now).
- Marketplace channel support list; offline editing scope beyond draft-preserving uploads.
- Demo data: six request stages need ≥6 fictional requests or explicitly empty stages (source has four).

## Added Sep 14 (Liquid Glass II build-out)
- Roles: owner / manager (Luis) / mechanic (Marco), each with desktop + phone. Verification stays owner-only. Permissions are per person: role defaults, every switch individually overridable (`team[].perms`).
- Sales tab: two pipelines (IRQ, Vehicle Sales), stages New Lead → In Conversation → Awaiting Deposit → Deposit Paid, Lost parked separately. Calls/meetings/follow-ups are timed tasks with reminder offsets, not stages. Deposit Paid hands off to the vehicle Sale tab / bidding.
- Tasks view across pipelines; in-app reminder banners; notification bell (desktop dropdown, phone sheet) with badge = overdue + due today.
- Agents screen: fixed left rail (desktop) / fixed-width cards (phone), live status lines, streamed replies, voice input. Agents propose; a person applies.
- Built out: Import requests (list + detail, agreement/deposit, candidates, bid approval), Tasks (all), Contacts, Activity (receipts in expanders), Finance (owner; manager read-only), Settings (connections, reminders, automation caps, recovery). Phone: vehicle detail, inbox thread, import requests, activity, settings; mechanic help + account.
- Email reminders designed (`AZKT Email Reminders.dc.html`); backend contract in `AZKT v2 Build Spec.md`.

## Still not designed
Shipment record, Listing/publication editor, Sign-in & role picker, record create/edit forms beyond lead/person/task, Procedures/Teach, Ask AZKT on phone, state matrix.
