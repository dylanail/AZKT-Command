# AZKT Operations Dashboard — Design Handoff Spec

**For:** Claude Design | **Source:** AZKT Build Packet v1.0 (Sept 11, 2026) | **Owner:** Dylan

You are producing high-fidelity, clickable mockups of an internal operations app for Arizona Kei Trucks (AZKT). This is not a marketing site and not a chatbot. It is a records-first business system with an AI manager built into it. Every screen must make the app's facts, sources, owners, and next actions more prominent than anything the AI is doing.

---

## 0. How to work from this document

### 0.1 Inputs you are getting
1. This spec.
2. Reference screenshots of platforms with a similar feel.

### 0.2 How to use the reference screenshots
Borrow from the references:
- Layout density and spacing rhythm.
- Navigation structure (sidebar + docked panel patterns, bottom nav on mobile).
- Card anatomy, board/list toggles, detail-sheet and drawer behavior.
- Table patterns, filter bars, inline action affordances.
- Mobile patterns for cards, sheets, and task completion.

Do not borrow from the references:
- Color palette, brand fonts, logos, iconography style.
- Any "AI theater": agent avatars, thinking animations, streaming token effects, glowing orbs, "agents collaborating" visuals, chain-of-thought panels.
- Metric-tile hero rows, giant KPI numbers, dark neon dashboards, heavy gradients.

If a reference conflicts with Section 2 (visual direction) or any "Must not" line in this spec, this spec wins. At the end, return a short note: for each reference, what you took from it and what you ignored.

### 0.3 Order of work
1. Component sheet (Section 3) first. Every screen reuses these components; do not invent per-screen variants.
2. App shell, desktop and mobile.
3. All P0 screens with their required state variants (Section 4).
4. The click-through flows in Section 5 for P0.
5. P1 screens, then P2 screens.

Deliver P0 as one complete set. Do not start P1 until P0 has been reviewed.

### 0.4 Fidelity and coverage
- Every screen at desktop (1440 wide) and mobile (390 wide).
- Every screen includes every state variant listed in its "Must show" block. A screen with only its happy-path state is incomplete.
- Use demo data that follows Section 6 exactly. Never use real customers, real inventory, real balances, or real emails.
- Where this spec does not decide something, decide it, and list the decision in the "Open decisions" note at the end (Section 7).

### 0.5 Definition of done for a screen
A screen is done when every line in its "Must show" block is visibly present in the mockup, every line in its "Must not" block is absent, and the mobile version works without drag-and-drop for any card movement.

---

## 1. Product context (read once)

**The business.** AZKT imports Japanese kei trucks and other JDM vehicles. Two ways vehicles enter: (a) AZKT buys inventory at Japanese auction through an exporter, ships to Arizona, reconditions, lists, and sells; (b) Import By Request (IBR): a buyer specifies what they want, pays a deposit, and AZKT sources a matching candidate at auction for them. The exporter communicates over Teams and delivers auction-sheet translations as Google Docs.

**The users.**
- Owner (Dylan): sees everything including costs and margins; approves consequential actions; directs the business by intent through the Ask panel.
- Employee (Shop / Mechanic role): sees their tasks, vehicle details, photos and documents. Never sees costs, margins, or unrelated customer/financial records. Primarily on mobile in the shop.

**The AI.** Seven logical roles (Manager, Customer & Sales, Sourcing/IBR, Logistics, Shop & Fulfillment, Listings, Finance & Documents) on one runtime. The Manager is the default conversation partner. The AI reads records, investigates, drafts, creates tasks, and proposes actions. Anything external (send, publish, request translation, bid, book, pay) goes through an approval card with the exact payload. The UI shows what the AI did as saved actions with receipts, not as a live feed of it thinking.

**Vocabulary the UI must keep distinct.** These are different objects and must never be visually collapsed into one:
- Case: an outcome open for days or months (a shipping quote, an import request).
- Task: one assigned next action with an owner and due time.
- Run: one finite burst of AI work.
- Action: one attempted external side effect, with a receipt.
- Commitment: a promised future outcome ("update by Friday").
- Approval: authorization bound to one exact payload version.
- Candidate → Match → Translation → Bid (sourcing chain).
- Shipment → Leg → Quote (logistics chain).
- Recon issue → Work order → Part (ordered / arrived / installed / verified).
- Listing package → Channel publication (per-channel state).
- Financial entry: estimated / quoted / invoiced / paid — four different things.
- Fact status: reported / inferred / confirmed / conflicted / outdated.

**Non-negotiable UI rules (apply to every screen).**
1. Every active case shows owner, next action, and next check or due time.
2. Every important fact can reveal its source, observed time, and status.
3. Proposed, requested, accepted, completed, and verified are shown as different states.
4. Physical work shows evidence (photo, note, receipt). Silence is never shown as done.
5. Approvals bind to an exact payload; a changed payload shows as invalidated.
6. "Nothing needs attention" cannot appear while any connection is expired or stale.
7. Estimated money never looks like actual money. A customer saying "I paid" is a reconciliation task, not a paid state.
8. Every card that can move has a non-drag "Move to…" control.
9. No invented deadlines. If a due time has no source, show "no due time set", not "soon".
10. Auction deadlines show Japan time and Arizona time.

---

## 2. Visual direction and design system

### 2.1 Mood
A calm, slightly characterful workshop desk. Warm neutral background, dark readable text, restrained accent colors, rounded cards, real vehicle photography, generous spacing. Light theme is the default and the only required theme.

One small cartoon kei-truck mascot illustration exists. It appears in exactly three places: the Ask panel empty state, the Now "all clear" state (only when every connection is healthy), and empty search / not-found states. It never appears in a vehicle card image slot, never conveys business state without text, and never animates on load.

### 2.2 Must not
Neon, gradients as surface fills, giant metric tiles, tiny labels, crowded tables, dark-mode-first "AI dashboard" aesthetic, agent avatars, typing/streaming theater, animated agent meetings, chain-of-thought panels, decorative charts, confetti, badges with more than three words.

### 2.3 Type and numbers
- One readable sans-serif for UI. Body text minimum 14px desktop, 16px mobile. The font stack must render Japanese glyphs cleanly; Japanese model names and translation excerpts appear in the product.
- Tabular numerals for money, times, and identifiers.
- Money always carries a currency code (USD, JPY). Estimated amounts are visually distinct from actual amounts (prefix "est." and muted weight). Never show a bare number for money.
- Times: relative + absolute on hover/tap. Auction deadlines show both JST and AZ (Arizona does not observe DST — label as "AZ", not "MST/PDT").

### 2.4 Status system (define once, reuse everywhere)
Build one badge component with a text label always present. Color is a reinforcement, never the only signal.

Health (the only four semantic colors in the product): On track · At risk · Blocked · On hold.

Everything else is a neutral badge with a text label, grouped by dimension. Do not color-code these individually.
- Vehicle Logistics: Purchase/Export · Awaiting vessel · In transit · At port · Domestic transport · Received.
- Vehicle Recon: Need Inspection · In Recon · Finalization · Ready for Sale.
- Vehicle Commercial: Inventory · Allocated (IBR) · Reserved · Sold · Delivered · Cancelled.
- Vehicle Documents: Missing · Under review · Ready · Exception.
- Request board: Inquiry · Qualification/Agreement · Deposit Pending · Active Search · Purchased/Fulfillment · Delivered/Closed, plus a Paused overlay badge.
- Candidate: Found · Needs Investigation · Translation Requested · Translation Ready · Customer Review · Bid Decision · Bid Placed · Won · Lost · Passed.
- Side effect / action: Proposed · Awaiting approval · Pending · Confirmed · Needs reconciliation · Failed · Cancelled.
- Run: Queued · Running · Waiting approval · Waiting input · Waiting external · Paused · Completed · Failed · Cancelled · Needs reconciliation.
- Fact status: Reported · Inferred · Confirmed · Conflicted · Outdated.
- Financial entry type: Estimated · Quoted · Invoiced · Paid.
- Part: Ordered · Arrived · Installed · Verified.
- Publication (per channel): Draft · Queued · Published · Failed · Needs cleanup.
- Skill: Proposed · Offline tested · Shadow · Supervised · Bounded automatic.
- Connection: Healthy · Degraded · Expired · Disconnected.

### 2.5 Photos
Vehicle image slots use real photography placeholders (any generic kei truck photo). The "No photo yet" state is a neutral placeholder with the text "No photo yet" and a "Request photos" action. Never fill an empty slot with the mascot or an icon of a truck.

### 2.6 Accessibility
WCAG AA contrast, 44px minimum tap targets, visible keyboard focus, text labels on all statuses, reduced-motion respected, every drag interaction has a button equivalent.

---

## 3. Global components (build these first)

### 3.1 Desktop app shell
- Left sidebar, restrained: Now · Work · History · System. Work expands to Vehicles · Import Requests · Tasks · Inbox · Customers. Nothing else in the sidebar. Translations, bids, shipments, listings are contextual subviews reached from records, never top-level nav.
- Top bar: global search (records only), a small sync-freshness indicator ("Synced 4 min ago" / "Gmail expired" as a warning chip), environment badge (Production / Staging — always visible), demo-data ribbon, user menu with role.
- Ask dock: right-side panel, collapsible, persists across navigation with its conversation and context chips intact. Default open on Now, remembers last state elsewhere.

### 3.2 Mobile app shell
- Bottom nav: Now · Work · Ask · More.
- Work opens a chooser: Vehicles · Requests · Tasks · Inbox · Customers. Inbox has a direct shortcut badge. The app returns to the last-used Work view.
- Record details open as full-screen sheets with a back control.
- Ask opens as a full-screen sheet with context chips pinned at top and the composer pinned at bottom.
- More: History · System · Approval queue · Sign out.

### 3.3 Vehicle card
Photo (or No-photo state) · Year Make Model (trim if any) · Allocation badge (Inventory / IBR: [Customer] / Reserved / Sold) · one plain-language situation sentence · Next: [action] · [owner role] · [due] · up to three badges (selected dimension state, health, one exception such as "Photos needed" or "Overdue"). Card footer: "Move to…" button, overflow menu (Ask about this, Add update, Assign task, Show sources).
Long names truncate on the second line with a tooltip; never overflow.

### 3.4 Request card
Customer name · requirements summary line (mandatory items only, e.g., "Manual · 4WD · A/C required") · budget (owner only) · deposit state · most urgent outstanding decision (e.g., "Bid decision due Fri 09:00 JST / Thu 17:00 AZ") · candidate counts by state ("1 translating · 1 bid decision") · Next / owner / due · Paused overlay when paused.

### 3.5 Candidate card
Thumbnail · auction ref + lot · year/model/grade · deadline (JST + AZ) · hard-requirement outcome chips per matched request (Pass / Fail / Unknown; Fail renders as a hard block) · candidate state badge · "2 requests matched" count without naming other buyers when viewed inside a request.

### 3.6 Task card
Next action (verb-first) · owner role and person if assigned · due · linked entity (vehicle/request/case) · blocker (if any) · dependency (if any) · evidence required (Photo / Note / Receipt / None) · "Assigned by" (person or agent role, with a one-line reason when agent). Actions: Mark done (opens evidence step when required), Blocked (requires reason), Reassign.

### 3.7 Approval card (most important component)
Order of content is fixed:
1. Recommendation, one sentence.
2. Exact consequence: what will happen, to whom, with what. Recipient / channel / amount + currency / auction + lot + max bid / listing + channel / vendor + booking scope, as applicable.
3. Sources and context, collapsed by default.
4. Metadata row (small): payload version, expires in [countdown], required policy version, requested by [agent role], case link.
Buttons: Approve once · Edit · Decline · Ask. A separate secondary link "Allow this class…" opens a distinct reviewed-change flow; it is never a one-tap toggle.
States: Pending · Expired · Invalidated ("Payload changed since you viewed — review again") · Approved (with receipt ID) · Declined · Executed with unknown result (Needs reconciliation).
Consequential classes (bid, book, pay, refund, price change, permission grant) never appear in a bulk-select. Ordinary classes (send message, request translation, publish under standing grant) may.

### 3.8 Provenance popover
Any fact value can be clicked/tapped. Shows: value · source (record, email, website, vendor message, manual entry, agent inference) · observed at · effective at (if applicable) · status badge (Reported / Inferred / Confirmed / Conflicted / Outdated) · entity version · "Verify" and "Show history" actions. Conflicted shows both values with their sources side by side.

### 3.9 Move-to gate dialog (non-drag movement)
Triggered from "Move to…" on any card, or from a desktop drag (same dialog either way). Shows target state, the gate requirements checklist with met/unmet marks and evidence links, and:
- If all met: Confirm.
- If any unmet: "Create task to resolve" (creates a task for the unmet item, card stays) or, owner only, "Override with reason" (required text field; the override is visible afterward on the timeline and on the card as a badge).
Backward moves always require a reason.

### 3.10 Detail sheet pattern
Header block (identity, allocation, health, situation sentence with "as of" time), quick-action row (Ask about this · Add update · Assign task · Show sources), tab strip. Same pattern for Vehicle, Request, Candidate, Customer, Shipment, Listing.

### 3.11 Context chips (Ask)
Removable chips above the composer: selected vehicle, visible object (the screen you're on), focused draft, recent explicit references. "Clear context" link. Chips persist while navigating; the visible-object chip updates as you move.

### 3.12 Empty / loading / error / stale states
One pattern for each, reused everywhere:
- Empty: short sentence + one primary action. Mascot only where Section 2.1 allows.
- Loading: skeleton in the shape of the content. No spinners on full screens.
- Error: what failed, when, what is still trustworthy, retry.
- Stale connection: warning banner naming the connection and what cannot be verified; page still renders.

### 3.13 Receipt toast
After any external action: "[Action] · [provider] receipt [ID]" with a link to History. If the result is unknown: "Result unknown — needs reconciliation" with a link, never a success toast.

---

## 4. Screens

Each screen lists: Purpose · Who · Layout · Content and interactions · States · Mobile · Must show · Must not. "Must show" is the acceptance checklist. Mock every line of it.

Priority: P0 = first deliverable. P1 and P2 follow after P0 review.

---

### P0-S1 · Now (owner home)

**Purpose.** What needs Dylan, what is moving without him, what is at risk. Read in 20 seconds.

**Who.** Owner. (Employee lands on Tasks instead; see P0-S4.)

**Layout.** Status line at the top: one or two sentences, source-backed, with a freshness indicator and any connection caveat inline. Below it, four sections in fixed order:
1. Needs your decision — approval cards (3.7) and decision requests.
2. Work moving without you — cases the AI or staff are progressing.
3. Waiting / at risk — waiting on external replies, overdue, approaching deadlines, blocked.
4. Recently completed — collapsed by default; each row has an actor and receipt ID.

Optional right rail (desktop): a compact text list of operational counters, each linking to a filtered Work view: Unowned work · Overdue commitments · Stalled recon · Awaiting approvals · Ready but unlisted · Unconfirmed payments. Text rows with counts, not tiles.

**Content and interactions.** Every item in sections 2 and 3 shows: what happened · why it matters · recommendation · owner · deadline (or "no due time set") · one primary action. Approval cards act inline. Any item opens its record. "Ask about this" on every item pre-loads the context chip. Sections collapse. Filter by role.

**States.**
- Healthy all-clear: connections healthy, no decisions pending. Mascot allowed. Status line still states what was checked and when.
- Degraded: a connection is expired. The all-clear cannot render. A banner names the connection and the status line says what cannot be verified ("Gmail not synced since Tue 14:10 — inbox items may be missing").
- Expired approval in section 1.
- Long list: sections become lists, never grids.

**Mobile.** Sections stacked, status line sticky, approval buttons full width, right rail becomes a collapsible "Counters" row.

**Must show.**
- Status line with freshness and a caveat variant.
- At least two approval cards of different classes in section 1: one "send customer message" and one "bid" — with visibly different consequence lines (recipient vs auction/lot/max JPY).
- Section 2 items each with owner and next check time, at least one owned by an agent role and one by a person.
- An at-risk item citing an auction deadline in both JST and AZ.
- Recently completed, collapsed, with receipt IDs when expanded.
- The degraded-state variant and the all-clear variant as separate mockups.

**Must not.** KPI tile row, live agent feed, "thinking" indicators, any deadline without a source, any chart.

---

### P0-S2 · Work › Vehicles

**Purpose.** Every purchased vehicle, viewed through one selected dimension at a time.

**Who.** Owner (full). Employee (no budget/cost data; same board).

**Layout.** Toolbar: dimension selector (Logistics · Recon · Commercial · Documents · Health; default Recon; last-used remembered) · Board/List toggle · filters (allocation, owner role, health, exception, search) · sort (due, age in state). Board columns are the states of the selected dimension. List view has at most seven columns: photo+identity · allocation · situation sentence · next/owner/due · health · selected-dimension state · age in state.

**Content and interactions.** Vehicle cards per 3.3. Click opens detail sheet (P0-S3). "Move to…" on every card opens the gate dialog (3.9); desktop drag opens the same dialog. Column headers show counts. Multi-select → Assign task / Ask about these.

**States.** Empty column · card with no photo · card with a long model/trim and long customer name · overdue card · blocked card · filter with no results · loading skeleton.

**Mobile.** Board shows one column at a time with a segmented control for the state; swipe between columns; "Move to…" is the only movement. List view is the default on mobile.

**Must show.**
- Board in the Recon dimension (all four columns populated unevenly).
- List in the Health dimension.
- The dimension selector visibly switching (two mockups).
- A "No photo yet" card with the request-photos action.
- The gate dialog open for a move to Ready for Sale with one unmet requirement (final photos) and the "Create task to resolve" option.
- The owner override-with-reason variant of that dialog, and the resulting override badge on the card.
- An "Allocated (IBR): [customer]" card visually distinct from "Inventory".
- Mobile single-column board and mobile list.

**Must not.** A single status field pretending to summarize all five dimensions. More than three badges on a card. Drag as the only way to move.

---

### P0-S3 · Vehicle detail

**Purpose.** Everything about one vehicle, organized by tab, every fact with a source.

**Who.** Owner (all tabs). Employee (Money tab hidden, cost fields hidden everywhere).

**Header.** Photo strip (or No-photo) · Year Make Model Trim · original chassis/frame identifier shown exactly as imported (e.g., "DD51T-284117 · source: exporter invoice") — not forced into a 17-character VIN box · allocation · the five dimension badges in a row · health · situation sentence with "as of [time]" and a subtle "regenerates when records change" hint · Next / owner / due. Quick actions: Ask about this · Add update · Assign task · Show sources.

**Tabs.**
1. Overview — key facts table (each value opens the provenance popover) · open tasks · open commitments · linked customer, request, shipment, listing · exceptions.
2. Timeline — chronological activity: actor (person / agent role / system), what changed, previous → new state, reason when required, evidence and receipt links. Backward transitions show the reason and actor prominently.
3. Work / Recon — recon issues as rows: symptom · inspection evidence · work order · owner · dependency · part status (Ordered → Arrived → Installed → Verified) · cost (owner only) · completion evidence (photo/note). "Silence" is not a state; an issue with no update shows "No update since [date]" and a nudge action. Disclosure flag on issues that must appear in the listing.
4. Photos / Documents — listing photo checklist (required shots, met/unmet, upload) · documents grouped by classification with status (Missing / Under review / Ready / Exception) · upload from camera on mobile · each document shows source and checksum-verified mark.
5. Money (owner only) — entries typed Estimated / Quoted / Invoiced / Paid with currency, source, external transaction ID · expected vs actual by category · exchange-rate source and date on JPY items · a "Customer reports paid — not settled" entry rendered as a reconciliation task, not a paid amount · totals separate estimated from actual.
6. Sale / Listings — canonical listing package version · approved price (change requires approval) · disclosures pulled from recon · per-channel publication rows: channel · state · URL · receipt · last verified · retry/cleanup action · reservation/sold sync state per channel.

**States.** Employee variant · sold vehicle with open title and aftercare tasks still visible · a conflicted fact (two sources, two values) · stale situation sentence ("Records changed since this summary — refreshing") · missing photo · document exception.

**Mobile.** Full-screen sheet, scrollable tab strip, camera upload on Photos/Documents, quick actions in a bottom action bar.

**Must show.**
- Provenance popover open on one fact, showing a "Reported" status from an exporter email.
- A conflicted fact with both values and sources side by side.
- Work/Recon with at least three issues at different part sub-states and one issue with completion photo evidence.
- Money tab with all four entry types, a JPY entry with exchange-rate source, and one "reported paid, not settled" reconciliation task.
- Sale/Listings with two channels: one Published with receipt, one Failed with a cleanup action; sync state visible.
- Timeline showing a backward transition (Ready for Sale → Finalization) with reason and actor, and one agent-actor row with a receipt.
- The employee variant of the same vehicle (no Money tab, no cost column in Recon).
- Mobile sheet with the camera upload action.

**Must not.** Twenty fields in the header. A "paid" state derived from an email. A single "synced" indicator across channels.

---

### P0-S4 · Work › Tasks

**Purpose.** Every assigned next action, grouped so unowned and overdue work is impossible to miss. Doubles as the employee's home screen.

**Who.** Owner (all tasks, all groupings). Employee (My tasks; no costs).

**Owner layout.** Group by: Role (Shop · Mechanic · Sales · Logistics · Finance · Owner · Agent) / Vehicle or case / Due. "Unowned" group always first and visually flagged. Filters: overdue, blocked, waiting on external, evidence required. Task cards per 3.6.

**Employee layout ("My tasks").** Overdue · Today · Upcoming. Each task opens a detail with: what to do (plain language) · vehicle context (photo, identity, bay/location if known) · evidence required · "Mark done" (opens evidence capture when required: photo, note, receipt) · "Blocked — I need…" (reason required, creates a blocker visible to owner and Manager) · "Ask" (scoped to this task).

**Content and interactions.** Tasks created by the AI show "Assigned by Manager (agent)" and a one-line why. Reassign, change due (owner), add dependency. Completing a task with evidence posts to the vehicle timeline.

**States.** Unowned group with items · overdue · blocked with reason · waiting on external ("Waiting on Montway reply, next check Tue 9:00 AZ") · evidence-required task at each step of completion · empty "My tasks" (employee).

**Mobile.** Employee view is the primary mobile design. Large tap targets, camera-first evidence capture, one-thumb completion.

**Must show.**
- Owner view grouped by role with an Unowned group first.
- A task requiring photo evidence, and the three-step completion flow (open → capture evidence → done) on mobile.
- A task marked Blocked with the reason visible on the owner side.
- A task assigned by an agent role with its reason line.
- A task waiting on external with its next check time.
- Employee "My tasks" desktop and mobile, with no cost data anywhere.

**Must not.** A "Done" state reachable without evidence when evidence is required. Any cost or margin in the employee view.

---

### P0-S5 · Ask (docked manager conversation)

**Purpose.** Direct the business by intent. One panel, available everywhere, scoped by explicit context chips.

**Who.** Owner (all agents, all actions). Employee (Manager and Shop only; read and task actions only).

**Anatomy.**
- Header: agent selector (Manager default · Customer & Sales · Sourcing/IBR · Logistics · Shop · Listings · Finance & Documents) as a compact dropdown, not tabs. Switching agents keeps the context chips and the shared records.
- Context chips row (3.11).
- Message list.
- Composer: text · push-to-talk mic · attach photo · "@" to reference a vehicle, customer, or request · send.

**Message types (design all six).**
1. Plain answer with a "Sources" expander listing the exact records read and their observed times.
2. Mission acknowledgement card: "Started: Get a shipping quote for 1997 Suzuki Carry (DD51T-284117) · Case #SHP-0031 created · Owner: Logistics · Next check: Tue 9:00 AZ" followed by progress summary lines as they happen ("Read shipment case", "Quote form lacks kei preset — using approved manual email to Montway", "Sent request · receipt MW-8841 · waiting"). Tool status, not thoughts.
3. Inline approval card (3.7).
4. Disambiguation card: "Which one? · 1997 Suzuki Carry (DD51T-284117, Inventory) · 1997 Suzuki Carry (DD51T-301552, IBR: [customer])" with tap-to-select. Shown whenever "this one" is unscoped and two records match.
5. Proposed record change card: field-level diff (before → after) with Apply / Discard; nothing changes until Apply.
6. Paused/limit card: "Paused — Gmail connection expired" or "Paused — case budget reached" with the action to fix.

**Behaviors to show.**
- Reload/resume: a "Resumed from saved activity" divider; the panel reconnects to the saved run, it does not start a new one.
- "Send it" with two drafts open → disambiguation, never the newest draft.
- Voice: push-to-talk button; transcription appears in the composer as editable text before sending; uncertain tokens (names, lot IDs, amounts, negations like "don't") are highlighted for confirmation; material actions still produce an approval card.
- Collapsed dock state on desktop: a slim edge with an unread/pending-approval count.

**Mobile.** Full-screen sheet; chips sticky at top; composer sticky at bottom; mic is large and one-thumb reachable.

**Must show.**
- All six message types in one or more mockups.
- The agent selector switched to Logistics with the same context chips still present.
- The disambiguation card for "this one needs tires" with no vehicle selected.
- Voice transcription with a highlighted uncertain amount ("¥1,200,000?") and an uncertain negation.
- The resumed-from-saved-activity state.
- The collapsed dock with a pending count.
- Employee variant with the reduced agent list.

**Must not.** Avatars, typing dots as theater, chain-of-thought, a "Send it" that acts on an ambiguous draft, a full-page chat layout on desktop (it is a dock).

---

### P0-S6 · Approval queue

**Purpose.** Every pending approval in one place, grouped by class. Feeds section 1 of Now.

**Who.** Owner only. Approvals happen only in the signed-in dashboard; the UI should make clear that email/SMS replies never count as approval (a short line at the top).

**Layout.** Groups: Messages · Publishing · Translation requests · Bids, bookings, payments · Permission changes. Cards per 3.7. Bulk select exists only in Messages and Translation requests. Filters: expiring soon, invalidated, by case, by agent role.

**Edit flow.** Edit opens the payload in a side panel; saving bumps the payload version and returns the card to Pending with a "v3 — re-review" marker.

**Must show.**
- At least one card in each of the five groups.
- One Invalidated card ("Payload changed since you viewed").
- One Expired card.
- Bulk-select active on Messages, and visibly absent on Bids/bookings/payments.
- The Edit side panel and the resulting version bump.
- The "Allow this class…" link opening a separate reviewed-change screen (one mockup: scope, limits, expiry, who is granting, confirm).

**Must not.** One-tap "always allow". Approve buttons on a consequential class inside a bulk action.

---

### P0-S7 · History

**Purpose.** Audit trail. Who or what did what, to which record, with what evidence.

**Who.** Owner (all). Employee (own actions and their vehicles' timelines only).

**Layout.** Filter bar: entity · actor type (person / agent role / system) · event type (record change / external action / approval / run / correction / override / connection event) · case · date range. Rows: time · actor · what · entity · evidence or receipt link · expand for detail. Drill-in opens Run detail (P2-S16) or a Correction record.

**Correction record (design the panel).** Original generated draft · final sent message · diff · classification chips (Style · Case fact · Customer exception · Policy proposal) · scope line ("Applies to: this customer's agreement only") · "Excluded from learning — uncertain match" variant.

**Must show.**
- A day of mixed rows: person edit, agent external action with receipt, system connection event, an override with its reason.
- One correction record panel with a Style change and a scoped Customer exception.
- The employee-limited variant.
- Filter bar with two filters applied.

**Must not.** A bare event log with no actor or evidence column.

---

### P0-S8 · System

**Purpose.** Health and control, readable in a minute, without becoming an infrastructure dashboard. Must render even if the AI model is unavailable.

**Who.** Owner only.

**Sections.**
1. Environment badge: Production / Staging, always visible here and in the shell.
2. Connections: Gmail · Exporter Teams · Google Docs translation watcher · Auction discovery · AZKT website · SMS · Calendar · Accounting/payments. Each: state badge (Healthy / Degraded / Expired / Disconnected) · last successful sync · what depends on it · Renew/Reconnect action.
3. Runtime: queue age · oldest waiting case (link) · failed jobs (link to dead-letter review) · unknown write outcomes needing reconciliation (link) · budget usage (per-run, per-case, global) · Kill switch: a deliberate control with a confirm step and a note that stopping does not undo completed external actions.
4. Standing grants: table of bounded permissions — class · scope (recipient/vendor/fields) · limits · granted by · granted at · expires · Revoke.
5. Skills: name · version · status (Proposed / Offline tested / Shadow / Supervised / Bounded automatic) · owner role · last replay result · Roll back.
6. Teach mode entry (P2-S17).

**Must show.**
- One Expired connection with its dependent features listed and a Reconnect action.
- The kill switch and its confirm dialog.
- Standing grants with at least two rows (e.g., "Nonbinding quote requests to Montway for active customers, approved fields only").
- Skills table with one skill in Shadow and one in Supervised, each with a version.
- A "Model unavailable" variant where connections and runtime still render.
- Staging badge variant.

**Must not.** Graphs of tokens, CPU, or latency. Anything that only an engineer would read.

---

### P0-S9 · Sign-in and role

Minimal: email + password or SSO button, environment badge, "Demo data" ribbon on demo. After sign-in, owner lands on Now; employee lands on My tasks. One mockup each. The role is visible in the user menu and cannot be switched by the employee.

---

### P1-S10 · Work › Import Requests (board and request detail)

**Purpose.** Every IBR buyer request through its lifecycle, with candidate work visible on the request without forcing the request into a candidate micro-stage.

**Board.** Columns: Inquiry · Qualification/Agreement · Deposit Pending · Active Search · Purchased/Fulfillment · Delivered/Closed. Paused is an overlay badge on a card, never a column. Cards per 3.4. Move via "Move to…" with a gate (e.g., Active Search requires agreement version and deposit evidence).

**Request detail tabs.**
1. Requirements — three tiers, visibly separated: Mandatory (cannot be relaxed by the AI; lock icon) · Preferences (weighted) · Exclusions (with reason and date). Budget (owner only). Edits are versioned with actor.
2. Agreement & deposit — agreement version, effective date, customer-specific exceptions labeled "scoped to this agreement"; deposit evidence entry (typed, with source).
3. Candidates — candidate cards (3.5) grouped by state; hard-requirement outcome chips per candidate; rejected candidates preserved in a collapsed group with reasons.
4. Communications — thread list linked to Inbox.
5. Timeline.

**Must show.**
- Board with one card in each column and one Paused overlay.
- A request whose card shows "1 translating · 1 bid decision" simultaneously, and the detail showing both candidates in those states.
- Requirements tab with the three tiers and a locked Mandatory item.
- The Active Search gate dialog with deposit evidence unmet.
- Rejected candidates group with reasons.

**Must not.** A request forced into a single candidate state. Any other buyer's name visible on a shared candidate.

---

### P1-S11 · Candidate detail, translation, and bid packet

**Purpose.** One auction candidate: what is known, what is unknown, how it scores against each matched request, translation status, and the exact bid packet.

**Header.** Thumbnail strip · auction reference + lot · listing link · source snapshot time · deadline (JST + AZ, countdown) · candidate state.

**Sections.**
1. Specifications — parsed fields; anything not confirmed shows "Unknown" as a value (e.g., A/C: Unknown), never blank and never assumed.
2. Requirement outcomes — table per matched request (this request named; others shown as "Request #2 (another buyer)"): each mandatory requirement → Pass / Fail / Unknown. Any Fail renders the candidate as blocked for that request regardless of suitability score; the score is shown greyed with "Blocked by mandatory: Transmission".
3. Suitability — rationale text with sources.
4. Translation — Requested (with Teams request receipt) → Document detected (version, last edit time) → Completion check: Complete / Still being edited / Identity mismatch → Extracted findings with source excerpt. A "Translation corrected — pending customer draft refreshed" notice when a newer version arrives.
5. Customer draft — the candidate presentation message for this buyer only, with approval card.
6. Bid packet — exact auction · lot · maximum amount + currency · fee basis · condition disclosures · expiry · authorization required; renders as an approval card of the consequential class. "Buyer is interested" is displayed as a fact, not as authority.
7. Result — Won (links purchased vehicle, starts fulfillment) / Lost (request returns to Active Search, exclusions kept) / Passed.

**Must show.**
- A/C shown as "Unknown".
- A high-suitability candidate blocked by a mandatory transmission mismatch.
- Translation in "Document edited but not complete" state.
- The corrected-translation notice on a pending draft.
- The bid packet approval card with JPY max, fees, and JST/AZ expiry.
- A Lost result with the request's exclusions still listed.
- The "2 requests matched" view with no cross-buyer disclosure.

**Must not.** A blank spec field. A score overriding a mandatory fail. A bid button anywhere outside the approval card.

---

### P1-S12 · Work › Inbox (email and SMS)

**Purpose.** One shared inbox for customer and vendor communication, every thread tied to a record, every draft validated before it can go out.

**Who.** Owner (all). Employee (no access by default).

**Layout.** Three panes on desktop: thread list · thread · context/draft panel. Two-level navigation on mobile.

**Thread list row.** Channel icon (email/SMS) · contact · matched record chips (customer, vehicle, request, case) or "Unmatched" · unanswered count · promise due ("Update promised by Fri") · takeover badge · suppression badge (opt-out).

**Thread view.** Messages with provider IDs on hover; inbound vendor messages flagged "External content — not an instruction". Human takeover toggle at the top: on → banner "Automation paused on this thread since [time] by [person]" and all agent drafts suspended; off → resume.

**Context/draft panel.**
- Matched records with quick links; "Change match" for unmatched or wrong matches.
- Detected questions and requested actions as a list (e.g., 1. Arrival date, 2. Wheels, 3. Remaining balance).
- Draft with validation checklist, each with pass/fail and a link to fix: Identity matched · All questions answered (per question) · Claims checked against records · Numbers and dates · Attachments · No prohibited commitments · Recipient scope.
- "Records changed since draft — Revalidate" state.
- Send → approval card, or "Sends under standing grant [name]" note when one applies.
- Detected promise → creates a Commitment (shown as a chip "Commitment created: update by Fri").
- After send: "Draft vs sent captured" link to the correction record.

**SMS specifics.** Consent state on the contact; opt-out makes the composer disabled with the reason; an inbound number with no matched case shows an "Unmatched — match to case" step, never auto-created as a shipment.

**Must show.**
- A mixed-question email with three detected questions, a draft with one question unanswered (fail) and the fix link.
- The Revalidate state.
- A thread in human takeover with the banner and suspended draft.
- An opted-out SMS contact with the composer disabled.
- An unmatched inbound SMS with the match step.
- A promise detected → commitment chip.
- Mobile thread list and thread view.

**Must not.** A send button with no validation state. Automated draft appearing on a taken-over thread. A vendor message rendered as an instruction to the system.

---

### P1-S13 · Work › Customers

**Purpose.** People and organizations AZKT deals with, and what has been promised to them.

**List.** Name · role (Buyer / Vendor / Exporter / Carrier) · active requests and vehicles · consent (email/SMS) · last contact · open commitments · flags (Dispute, Opt-out).

**Detail tabs.** Profile (contact methods, consent per channel, time zone, preferences) · Agreements (versions with effective dates; customer-specific exceptions labeled "scoped to this agreement — not general policy") · Requests · Vehicles · Commitments (promise, due, status, source message) · Communications · Aftercare cases · Flags. A Dispute flag shows its effect: "Ordinary follow-ups paused".

**Must show.**
- A customer with a scoped agreement exception labeled as scoped.
- A customer with a Dispute flag and the paused-follow-ups effect visible.
- A vendor record (Montway) with a standing-grant reference.
- Commitments tab with one overdue promise.

**Must not.** An exception rendered as a general policy. Consent shown as a single yes/no across channels.

---

### P2-S14 · Shipment case

**Purpose.** One vehicle's movement as separate legs, each with its own state and evidence, plus the quote decision.

**Sections.**
1. Legs — Purchase/Export processing · Awaiting vessel · In transit (vessel, voyage) · At port (discharge, release, storage deadline) · Domestic transport (carrier, pickup appointment, driver) · Received (evidence). Each leg: state · source · evidence · next action. "Vessel arrived", "Discharged", "Released", "Carrier booked", "Received" are separate rows, never one status.
2. Quotes — quote cards: vendor · scope · amount + currency · inclusions · extra fees · service type · timing · expiry · conditions · source message link · Estimate vs Binding label.
3. Comparison — evidence basis with explicit strength ("2 comparable completed shipments, 2024 — weak evidence") and a recommendation that may be "get a second quote". Quotes and final charges shown in separate columns.
4. Decision card — the quote, its uncertainties, the recommendation, and the exact customer message → approval card. Forwarding is the only thing it approves.
5. Book transport — a separate action with its own approval card; visibly not part of the forward approval.
6. Waiting — when waiting on a vendor: correspondence IDs, pending questions, follow-up time, "Ambiguous reply — clarification requested" variant.
7. Pickup and delivery — driver SMS thread link, appointment with conflict warning against the calendar, delivery evidence upload.

**Must show.**
- The manual-quote fallback note ("Online form had no kei truck preset — used approved manual email to Montway").
- A weak-evidence comparison recommending a second quote.
- Forward and Book as two separate approval cards.
- A waiting state with next check time surviving a "Resumed" marker.
- An appointment conflict warning.

**Must not.** A booked state implied by a forwarded quote. A substitute vehicle model in a quote.

---

### P2-S15 · Listing package and publication

**Purpose.** One canonical listing per vehicle; publication tracked per channel.

**Sections.** Package version and diff since last publish · listing class (En-route vs Ready-for-sale, each with its own gate; en-route copy must not contain arrival dates unless sourced) · disclosures pulled from recon (read-only here, edited on the vehicle) · approved price (change opens a consequential approval) · photo checklist · channels: website · supported marketplaces · unsupported channel (renders as "Assigned to [person] — package attached", never as published) · per-channel state, URL, receipt, last verified, retry/cleanup · reservation and sold sync per channel.

**Must show.**
- An en-route listing with "arrival: not confirmed" rather than a date.
- Partial publish: website Published, one marketplace Failed with cleanup action.
- An unsupported channel rendered as a human task with the package attached.
- A sold vehicle with one channel "Needs cleanup".

---

### P2-S16 · Run detail (from History)

**Purpose.** One finite AI run, inspectable.

**Content.** Trigger · linked case · state · checkpoints list · tool records (tool name, argument summary, result summary, receipt, duration) · selected versions (model adapter, skill version, policy version) · cost · result with evidence · proposed record changes · unresolved questions · Cancel (with "cannot undo completed external actions" note). Needs reconciliation state: "Result unknown for [action] — verify at provider before retry", with a Reconcile action.

**Must show.** A completed run with two tool receipts · a Waiting external run · a Needs reconciliation run · the cancel confirm.

---

### P2-S17 · Teach mode

**Purpose.** Turn a demonstration or correction into a versioned, testable skill without granting it authority.

**Flow.** Input (upload recording / screenshots / example thread, or write a direct correction) → Proposed skill card: goal · relevant fields · normal route · decision points · failure alternatives · permission boundary (read-only display: "Uses existing Logistics permissions; cannot grant new ones") · examples · completion evidence · tests → Questions (only unresolved material decisions, each with a why) → Save as Proposed → status pipeline Proposed → Offline tested → Shadow → Supervised → Bounded automatic, each step showing its evidence (replay results, shadow comparison) → Roll back (keeps source history).

**Must show.** The proposed skill card · one material question with its why · a failed replay blocking promotion · the rollback confirm.

**Must not.** A permissions field the skill can edit. A "promote" button without replay evidence.

---

### P2-S18 · Standing grants and skill versions (System subviews)

Detail views for rows in P0-S8 sections 4 and 5. Grant detail: class, exact scope, limits, granting reviewer, effective and expiry, usage log with receipts, Revoke. Skill detail: version history, diff between versions, tests, replay results, shadow results, promotion history, Roll back.

---

## 5. Click-through flows (mock these as linked screens)

These are the functionality checks. Each flow must be navigable in the prototype. Flows F1–F5 are P0; F6–F9 follow with P1/P2.

**F1 · Gate on card move (P0).** Vehicles board (Recon) → "Move to…" on a card In Recon → target Ready for Sale → dialog shows "Final photos: 2 of 6" unmet → "Create task to resolve" → task appears in Tasks under Shop with Photo evidence required → card stays In Recon with a "Photos needed" badge.

**F2 · Approval with edit (P0).** Now → approval card "Forward Montway quote to [customer]" → Edit → change one sentence → payload version increments, card returns to Pending marked v2 → Approve once → receipt toast with provider ID → History row with the receipt.

**F3 · Scoped vs unscoped conversation (P0).** Vehicle detail open → Ask: "this one needs tires" → proposed record change card (adds recon issue) → Apply → issue appears in Work/Recon. Then: Now (no vehicle selected) → same message → disambiguation card listing two similar Carrys → select one → same result.

**F4 · Employee completes with evidence (P0, mobile).** My tasks → task "Confirm A/C repair complete" (Photo required) → Mark done → camera capture → note → Done → vehicle timeline shows the completion with the photo and the employee as actor.

**F5 · Expired connection (P0).** System shows Gmail Expired → Now cannot render all-clear; banner names Gmail; Inbox shows "Not synced since [time]"; Ask shows the paused/limit card if asked about new mail → Reconnect → states clear.

**F6 · Mixed-question email (P1).** Inbox → email asking about arrival, wheels, balance → context panel lists three questions → draft checklist shows "Question 3 unanswered" → fix → all pass → Send → approval card → approve → "Draft vs sent captured".

**F7 · Human takeover (P1).** Inbox thread → toggle takeover → banner, draft suspended → owner replies manually → History shows the manual send → toggle off → automation resumes with a note.

**F8 · Candidate with unknown and mandatory fail (P1).** Request detail → candidate with A/C Unknown and Transmission Fail → blocked state despite high score → owner asks Sourcing "why" → answer cites the mandatory rule and the source page.

**F9 · Sold event and channel cleanup (P2).** Vehicle marked Sold via gate → Sale/Listings shows website updated, one marketplace Failed → cleanup task created → queued "still available" reply in Inbox is shown cancelled.

---

## 6. Demo data rules

Everything is fabricated and labeled. A thin "Demo data" ribbon is visible on every screen.

**Vehicles (use these ten).** Frame identifiers are Japanese-style, not 17-character VINs.
1. 1997 Suzuki Carry — DD51T-284117 — Inventory — In Recon, A/C repair outstanding, photos 2/6 — health At risk.
2. 1995 Honda Acty — HA4-1102938 — Inventory — Ready for Sale — listed on website, one marketplace Failed — On track.
3. 1998 Daihatsu Hijet Jumbo — S110P-088231 — Allocated (IBR): a customer with a long name (e.g., "Maria Fernanda de la Cruz-Whitaker") — Finalization — On track.
4. 1994 Subaru Sambar — KS4-201776 — Inventory — Need Inspection — No photo yet — On hold.
5. 1996 Mitsubishi Minicab — U42T-330915 — Reserved — In Recon — Blocked: waiting on part ETA (part Ordered) — Blocked.
6. 1999 Suzuki Every — DA52W-118204 — Sold — Delivered — open title follow-up task and aftercare case — On track.
7. 1997 Suzuki Carry — DD51T-301552 — Allocated (IBR) — Awaiting vessel — (second Carry, used for disambiguation) — On track.
8. 1993 Honda Acty Street — HH4-0723316 — Inventory — At port, storage deadline in 3 days — At risk.
9. 1995 Toyota Hilux Surf — KZN185-9013377 — Inventory — In transit — long trim string "SSR-X Wide Body 3.0 Turbo Diesel" — On track.
10. 1992 Suzuki Jimny — JA11-247501 — Inventory — Ready for Sale, unlisted for 11 days — At risk.

**Customers (six).** Mix of buyers and vendors: two IBR buyers (one with a scoped agreement exception), one inventory buyer in Dispute (aftercare), one SMS contact opted out, one exporter ("Exporter (Teams)" — do not name a real company), one carrier (Montway, named in the source packet; this is fine). Use fictional personal names and emails on a demo domain (example.com).

**Import requests (four).** One in each of Qualification/Agreement, Deposit Pending, Active Search (with two candidates: one Translation Requested, one Bid Decision), Purchased/Fulfillment. One Paused overlay.

**Candidates (four).** Auction references are generic ("Auction A · Lot 4021"); do not name a real auction house. Deadlines in JST with AZ conversion. One with A/C Unknown. One with a mandatory transmission Fail and high suitability. One Lost.

**Money.** JPY for auction/bid/exporter items with an exchange-rate source and date; USD for domestic items. Include all four entry types on vehicle 1. Include one "customer reports paid — not settled" reconciliation task on vehicle 6.

**Times.** Now = a weekday morning in Arizona. Show at least one deadline as "Fri 09:00 JST · Thu 17:00 AZ".

**Connections.** Gmail Expired in the degraded variants; everything else Healthy.

**Never include.** Real customers, real inventory, real balances, real emails, real auction house names, real people's names, and any dollar figure presented as an actual AZKT number.

---

## 7. What to return

1. Component sheet: tokens (color, type, spacing), the status badge system from 2.4, and every component in Section 3 with its states.
2. P0 screens (S1–S9), desktop and mobile, with every "Must show" variant, plus flows F1–F5 as linked screens.
3. P1 screens (S10–S13) and flows F6–F8.
4. P2 screens (S14–S18) and flow F9.
5. Reference notes: per reference screenshot, what was taken and what was ignored.
6. Open decisions: every choice made that this spec did not specify, in one list, so they can be confirmed or reversed.

---

## 8. Every-screen checklist (apply before marking any screen done)

- [ ] Every active case or task shows owner, next action, and next check/due (or "no due time set").
- [ ] Every fact value has a provenance popover; at least one is shown open somewhere in the set.
- [ ] Estimated, quoted, invoiced, and paid never look the same.
- [ ] Reported, inferred, confirmed, conflicted, and outdated are distinguishable by label.
- [ ] No "all clear" while any connection is expired.
- [ ] Every movable card has "Move to…"; mobile has no drag-only interaction.
- [ ] Employee variants contain no costs or margins.
- [ ] No deadline appears without a source; none say "soon".
- [ ] Auction deadlines show JST and AZ.
- [ ] Every external action shows a receipt or an explicit "unknown result".
- [ ] Approval cards lead with the recommendation, then the exact consequence, with Approve once / Edit / Decline / Ask.
- [ ] Empty, loading, error, and stale-connection states use the shared patterns.
- [ ] Demo-data ribbon and environment badge are visible.
- [ ] No agent theater anywhere.
- [ ] Mascot appears only where Section 2.1 allows.
