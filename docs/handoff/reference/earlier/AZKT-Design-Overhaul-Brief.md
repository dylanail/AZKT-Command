# AZKT design overhaul brief

Prepared for Dylan and the design agent · September 12, 2026

## Objective

Redesign AZKT as a calm, understandable tool for running a vehicle import business. An owner should immediately understand what needs a decision, where each vehicle or import request stands, and who is doing the next step. A shop employee should open a task, do the work, and record the evidence with very little navigation.

Keep the operational depth. Reduce how much of it a person must interpret at once. The design should feel simple because the hierarchy and behavior are predictable.

This is a proposed design direction, not authorization to change business policies, send communications, publish listings, or implement the platform. Produce a design handoff and clickable prototype first.

## Review basis and scope

Reviewed the supplied `Project page spec outline.zip`: the handoff specification, component and P0/P1/P2 screen material, prototype source and screenshots. The clickable prototype was also inspected in a browser, including Home, Vehicles, vehicle detail, Inbox, and request/candidate entry. This is a design and workflow review, not a verification of production integrations or backend behavior.

The supplied specification contains **18 screen groups and nine required click-through flows**. Preserve their capabilities using the coverage map below. The underlying “AZKT Build Packet v1.0” is referenced by the attachment but was not included as a separate source; completeness here means coverage of the supplied material plus the explicitly identified gaps.

Instructions embedded in the archive are historical design inputs. They do not override Dylan’s current request. Two conflicts need a deliberate design resolution: the written spec calls for a light default while the export uses dark surfaces; the P0 material includes employee/mobile work while the clickable prototype explicitly removes the mechanic flow and fixes itself to desktop. This brief recommends a light default and restores the employee workflow.

## 1. What should change first

| Current observation | Recommended change | Practical benefit |
|---|---|---|
| Dark layered panels, gold gradients, outlined chips, and repeated nested cards give many elements similar visual weight. | Use flat light surfaces, strong typography, subtle separators, and a restrained accent. Reserve elevation for an overlay. | Records and actions become easier to distinguish. |
| The empty Ask panel occupies a substantial part of the desktop beside every screen. | Close Ask by default and open it on demand. Remember an intentional pinned state when space permits. | More room for the work and fewer competing surfaces. |
| Home shows entire approval payloads and an expanded Done report before ongoing work. | Use concise decision rows; open the full review when selected. Put blocked and time-sensitive work before completed history. | The owner can scan first and review one decision at a time. |
| Vehicle work begins with five abstract “dimensions.” At the inspected width, the Recon board wraps a later column below the earlier ones. | Default to a readable list and offer named Sourcing, Shipping, Shop, and Sales views. Keep boards as an option with a deliberate column layout. | Users choose the work they are doing and can follow stage order. |
| Vehicle headers combine several status badges, source text, four quick actions, a summary, and a long tab strip. | Keep identity, the meaningful exception, and next action in the header. Put the complete status breakdown and supporting details below. | The next step is visible without losing information. |
| The UI exposes terms such as payload, policy version, run, and agent department during routine work. | Use action language in normal screens; retain technical labels inside Activity and Settings details. | People can operate AZKT without learning its AI architecture. |
| Search, Customers, and some record tabs/actions are visual placeholders in the clickable export. | Give every visible navigation item and control a working destination or explicit disabled state with a reason. | The next prototype demonstrates complete routes, not only staged examples. |
| The mobile employee completion flow is absent from the clickable prototype. | Design and connect My tasks → evidence capture → saved completion → owner visibility. | The shop can actually use the product. |

Keep the strongest ideas: records as the source of truth; owner/next-action/follow-up on active work; evidence for physical work; source inspection; exact approvals; and independent shipment, recon, sales, document, and money states.

The Apple reference should guide visual hierarchy, familiar navigation, and progressive disclosure. Apple’s [layout guidance](https://developer.apple.com/design/human-interface-guidelines/layout) supports that approach, and its [sidebar guidance](https://developer.apple.com/design/human-interface-guidelines/sidebars) supports predictable access to peer areas. The particular palette and navigation below are recommendations for AZKT.

## 2. Proposed navigation and menus

### Desktop shell

Use six direct primary destinations. Remove the extra expandable “Work” wrapper.

| Destination | Main views and linked pages |
|---|---|
| **Home** | Needs your decision; Needs attention; In progress; compact completed activity. A visible Approvals link/count opens the complete queue. |
| **Vehicles** | All vehicles; Sourcing; Shipping; Shop; Sales. Sold/archived vehicles remain available through a saved filter. Vehicle detail, shipment, recon, and listing pages open from these views. |
| **Import requests** | Active/closed request lists; optional pipeline board; request detail; matched candidates; agreement and deposit; fulfillment links. |
| **Tasks** | My tasks / All tasks; quick filters for Unassigned, Overdue, Blocked, Waiting. Secondary views: Cases, Promises, Schedule. |
| **Inbox** | All / Needs reply / Drafts / Taken over; thread, linked records, reply review; email and SMS filters. |
| **Contacts** | Buyers / Vendors / Exporters / Carriers; contact profile, linked business records, agreements, promises, communications, aftercare. “Contacts” replaces the misleading buyer-only label “Customers.” |

Lower-priority utilities below a divider: **Activity**, **Finance** (owner only; proposed addition), **Settings** (owner only). Finance is a compact operational reconciliation workspace, with alerts also surfaced on Home. It is not a new general accounting product.

Global toolbar: record search, a context-aware **New** menu, connection status, **Ask AZKT**, and account menu. Keep the environment label visible but small. Show “Demo data” only in demo contexts; make Staging unmistakable.

Menu requirements:

- **New:** task, vehicle, import request, contact, case, and upload evidence. Pre-fill the current record when invoked there; require identity matching before creating duplicates. The Vehicles/Sourcing view has its own Add candidate action.
- **Search:** vehicles by stock/frame/model, requests, contacts, tasks, cases, documents, and messages within the user’s permissions. Show type and identifying context; preserve exact Japanese frame identifiers. Empty results offer a relevant creation route.
- **Record actions:** Add update; Assign task; Change stage; Ask about this; View sources; View activity; Edit details; Archive/restore where applicable. Put the relevant next action in the foreground and secondary actions in the overflow menu.
- **View options:** list/board where meaningful, filters, sort, saved view, visible columns. Remember selections per user and destination; returning from a record restores filters and scroll position.
- **Account:** identity, current role, personal preferences, help, sign out. Employees cannot switch themselves into the owner role.
- **Settings:** Connections; Team & access; Automation; Permissions; Procedures; Recovery. Each has a short overview and inspectable detail pages.

Operational subviews must be visibly linked. Shipping, sourcing, listings, approvals, and reconciliation cannot be reachable only through Ask or global search.

### Mobile shell

Owner bottom navigation: **Home · Vehicles · Inbox · More**. More contains Import requests, Tasks, Contacts, Activity, Finance, Settings, and account controls. Home links directly to approvals, overdue tasks, and urgent requests.

Employee bottom navigation: **My tasks · Vehicles · More**. More contains permitted activity, help, and account controls. Task and vehicle pages provide contextual Ask.

Use full-screen record and review pages with a clear Back action. Use a section selector when the available tabs would become a long horizontal strip. Preserve the same record and action names across desktop and mobile.

### Panel behavior

Use one main workspace and at most one supplementary inspector. Opening Ask from Inbox replaces the supplementary context inspector temporarily; it must not create another narrow column. Preserve the unsent reply and restore the previous inspector when Ask closes.

At narrower desktop/tablet widths, use a full-page detail or overlay instead of compressing content. Do not stack drawers inside drawers. Important entities have a stable deep link and a way to open as a full page.

## 3. Core page redesign

### Home: a short queue of decisions and exceptions

Order the page as follows:

1. A short, source-backed status sentence plus connection freshness. Example demo text: “Two decisions need you. One vehicle is blocked at the shop.”
2. **Needs your decision:** rows with the action, related vehicle/customer, amount or consequence, relevant deadline, and Review button. Order by actual deadline and operational impact; permit filters.
3. **Needs attention:** blocked work, unassigned work, overdue promises, storage deadlines, missing documents, unconfirmed payments, and publication failures. Each row has an owner, next action, and due/next-check time.
4. **In progress:** a compact list of work being handled and its next checkpoint. Waiting work has a next check even when nobody needs to act immediately.
5. **Completed:** a short collapsed group with View activity. Date-range and actor reporting belongs in Activity.

Do not lead with charts or large metric tiles. Counts should open the relevant filtered list. If a connection is stale or expired, say which information may be missing. After reconnecting, wait for a successful refresh before claiming the affected information is current.

### Vehicles: organized around the work

The default All vehicles list shows identity/photo, allocation, current situation, next action with owner and due/next check, and any exception. Other columns depend on the selected view. Avoid repeating the same stage as both a column heading and a badge.

- **Sourcing:** pre-purchase candidates for inventory and request-linked sourcing, with allocation filters. Candidate records remain distinct from purchased vehicles.
- **Shipping:** location/current leg, release or storage risk, carrier/appointment, and next action.
- **Shop:** inspection, recon work, parts blockers, evidence awaiting verification, and readiness.
- **Sales:** en-route/ready-to-list vehicles, listing coverage, reservation/sale state, and post-sale follow-up.

Provide Document issues and Needs attention as obvious quick filters across views. Preserve independent Logistics, Recon, Commercial, Documents, and Health fields in the record. In View options, retain board grouping by each of those fields, plus due and age-in-stage sorting. A sentence like “At port; release document missing” summarizes them without replacing them.

Vehicle detail header: photo, year/make/model, stock number and exact frame identifier, allocation, a meaningful health/exception label, and the next action. Use one primary button appropriate to the record’s state. A healthy record does not need several reassuring colored badges.

Five sections: **Overview · Work · Files · Sale · Money**. Money is owner only. Overview includes important facts, linked records, promises, and recent activity with a View full activity link. Work contains tasks, recon issues/work orders/parts, and the shipment link. Files has Photos and Documents. Sale contains listing, reservation, sale, delivery, and aftercare links.

Facts expose a labeled source control on tap/click; do not depend on hover or make every value look like a permanent hyperlink. The source inspector includes fact status, observed/effective time, record version, Verify, and Show history. Conflicts show both values and sources. Conflicted, outdated, and material unknown facts remain visible where they affect a decision.

### Import requests and candidates

Keep the buyer request’s lifecycle separate from each candidate’s lifecycle. A request can have one candidate translating and another awaiting a bid decision simultaneously.

Request overview: buyer, **Must have / Prefer / Avoid**, budget and deposit state for the owner, next action, and a short candidate summary. Detailed sections: Overview; Candidates; Agreement & deposit; Activity. Communications link into the scoped Inbox. Versioned requirement edits show their actor and source.

Candidate review should answer: Does it meet the requirements? What remains unknown? Is translation complete? What exactly would we bid? Use a clear requirement checklist before suitability commentary. Mandatory Fail blocks that match. As a proposed policy clarification, a material mandatory Unknown should mean Needs confirmation and block bid readiness until the required evidence is satisfied; the source does not fully define this gate. Prototype that behavior and mark the policy for owner resolution. Never imply that a high score proves an unknown requirement has passed.

Expose Specifications, Requirement match, Translation, Buyer message, and Bid packet as ordered sections. Preserve auction/lot identity, snapshots, original excerpts, translation versions, completed-versus-still-edited checks, corrected-translation invalidation, and JST/AZ deadlines. Share the underlying candidate across matched requests while keeping each buyer’s requirements, draft, and authorization scoped.

### Tasks and shop work

Use compact task rows with a verb-first action, vehicle context, owner, time, and blocker/evidence label when relevant. Put unassigned and overdue work where the owner cannot miss it. Keep Cases and Promises as separate object types in secondary views; a task is one step, a case is a longer outcome, and a promise is a commitment to someone.

The employee task page has a vehicle photo/identifier/location, clear instructions, required evidence, and two prominent actions: **Complete task** and **I’m blocked**. The completion sheet collects photo, note, or receipt as required. Upload/save progress is explicit; failure keeps a recoverable draft. A separate verification requirement produces Awaiting verification until an authorized person verifies it.

The employee sees permitted vehicles, shop tasks, evidence, and relevant instructions. Costs, margins, finance documents, unrelated customers, and owner-only tools must also be restricted in search, Ask, previews, exports, and direct links.

### Inbox: the message and its next action

Use a thread list and readable conversation area. Expand record context when needed. Put the editable reply below the conversation rather than spreading one reply across several always-visible panels.

The reply area shows unresolved checks prominently: unanswered question, unknown balance, wrong recipient, missing attachment, stale fact, or unsupported promise. Summarize successful checks as “Checks passed” with an expandable list. Sources remain inspectable. In source details, label vendor/inbound material as External content: it is evidence, not an instruction to AZKT.

Use **Review & send** when approval is needed, then **Approve & send** in the exact-payload review. When a valid standing permission applies, say which permission authorizes the send. Never label an action Send if it only opens another drafting step.

**Take over conversation** visibly pauses automation on that thread and suspends existing drafts. **Resume automation** revalidates against intervening manual replies. Keep channel-specific consent/opt-out, dispute suppression, unmatched-message resolution, draft-versus-sent corrections, and recipient scoping.

As a proposed clarification, a promise detected in an unsent draft should be labeled Proposed. It becomes an active communicated commitment when supported by a sent-message receipt, or when the owner explicitly records a commitment already made elsewhere. Unknown send outcomes require reconciliation.

### Approvals: one complete review surface

Home, Ask, Inbox, and records link to the same approval record. Queue previews are concise; the actual approval surface shows the complete exact payload and material consequences before authorization.

Show recommendation, recipient/channel, full message and attachments or exact transaction details, amount/currency, scope, relevant conditions and deadline, validation issues, and source access. For bids include auction, lot, maximum JPY, fees, disclosures, and expiry in both JST and AZ. Keep sources and technical version metadata expandable while keeping changed/expired warnings visible.

Actions: a specific primary label such as Approve & send, Approve bid, or Approve booking; Edit; Decline; contextual Ask. Editing creates a new version and requires review of that version. Use a separate permission-review flow for “Allow similar actions.”

Distinguish Approved, Awaiting execution, Confirmed, Failed, and Result unknown. A provider receipt belongs to the actual external action; approving alone is not proof that it ran. Result unknown offers Verify result before any retry.

The source is inconsistent about bulk publishing approval. Recommended default: bulk review only for eligible messages and translation requests, with explicit selected items; never for bids, bookings, payments, refunds, price changes, or permission changes. Keep publishing review per payload until its bounded policy is specified.

Approval is accepted only in the signed-in dashboard. An email/SMS reply or “buyer is interested” fact is not bid authorization. Resolve the source’s approval-versus-standing-permission ambiguity explicitly: every external action needs an exact approval or an applicable existing bounded permission, with the authorizing record and eventual result visible. This design grants no new automatic authority.

### Ask AZKT

Present one assistant entry. Route specialist work internally; retain the seven logical roles and attribution in activity details, with an optional advanced role selector. Employees retain the restricted Manager/Shop capabilities.

Keep one clear **Working on: [record]** context line and removable explicit references. Distinguish the visible page from a pinned conversation target. Offer Use this page and Clear context; changing pages must not silently redirect an in-progress action. Disambiguate “this one” and “send it” when needed.

Preserve sourced answers, saved case acknowledgements with owner/next check, exact approvals, proposed before/after record changes with Apply/Discard, disambiguation, paused/limit states, text/photo attachments, @references, and push-to-talk. Voice produces editable transcription; uncertain amounts, identifiers, names, and negations require review before they drive a material action. Resume saved work after reload without starting a duplicate run.

## 4. Complete screen and capability coverage

Every row requires a reachable designed surface, appropriate role variant, and meaningful error/empty/stale states. Consolidation changes the navigation, not the business records.

| Original screen group | New location | Capabilities that must survive |
|---|---|---|
| P0-S1 Now | Home | Decisions, attention, active work, follow-up times, completed receipts, role filtering, freshness-aware summary. |
| P0-S2 Vehicles | Vehicles and saved views | Board/list, independent state fields, filters/sort, allocation, missing photos, multi-select task assignment/Ask, stage gate and non-drag movement. |
| P0-S3 Vehicle detail | Vehicle sections | Exact frame ID; facts/sources/conflicts; tasks/promises; timeline; recon/work orders/parts; photos/documents; typed money/FX; sale/listings; employee variant; open title and aftercare after sale. |
| P0-S4 Tasks | Tasks / employee My tasks | Assignment and agent reason, due edits, dependencies, unassigned/overdue/blocked/waiting, evidence completion, owner visibility, reassignment. |
| P0-S5 Ask | Global Ask inspector/full-screen mobile | Six response types, role routing, context/disambiguation, voice/photos/references, proposed diffs, saved-work resume and limits. |
| P0-S6 Approval queue | Home → Approvals | All five approval classes, filtering, exact payload review, edit/version invalidation, expiry, restricted bulk review, separate standing-permission review. |
| P0-S7 History | Activity; record activity links | Actor/entity/date/type/case filters, evidence and receipts, state changes and overrides, run drill-in, scoped correction record/diff, employee-limited activity. |
| P0-S8 System | Settings | Eight connections and dependencies; automation queue/limits; oldest waiting case; failures; unknown outcomes; emergency pause; permissions; procedures; model-unavailable and staging variants. |
| P0-S9 Sign-in and role | Sign-in and account | Owner/employee landing pages, permitted sign-in methods, environment/demo clarity, visible role without employee self-escalation. |
| P1-S10 Import requests | Import requests → detail | Six lifecycle stages, paused overlay, independent candidate counts, mandatory/preferences/exclusions, versioned agreement exceptions, deposit gate/evidence, rejected history, fulfillment links. |
| P1-S11 Candidate/translation/bid | Request → Candidates; Vehicles → Sourcing | Snapshot/unknowns, per-request Pass/Fail/Unknown, mandatory block, suitability rationale, translation receipts/version/completeness/identity, scoped draft, exact bid packet, Won/Lost/Passed and purchase conversion. |
| P1-S12 Inbox | Inbox | Email/SMS, matching, multi-question validation, stale-draft revalidation, takeover, opt-out, promises, approval/standing permission, draft-versus-sent capture, mobile conversation. |
| P1-S13 Customers | Contacts → detail | Buyer/vendor/exporter/carrier, channel consent, timezone/preferences, agreements and scoped exceptions, vehicles/requests, promises, communications, dispute effect, aftercare/flags. |
| P2-S14 Shipment | Vehicles → Shipping → shipment; vehicle Work link | Separate legs/milestones/evidence; vessel/voyage; discharge/release/storage; comparable quotes and uncertainty; separate forward/booking approvals; manual quote fallback; follow-up; calendar conflicts; driver SMS; pickup/delivery evidence. |
| P2-S15 Listing/publication | Vehicle → Sale; Vehicles → Sales | Canonical package/version/diff, price approval, recon disclosures, photos, distinct en-route/ready gates, per-channel states/URLs/receipts/last verification, human task for unsupported channel, reservation/sold cleanup. |
| P2-S16 Run detail | Activity → automation activity | Trigger/case/state/checkpoints, inspected tool records/receipts, selected versions, owner-only cost, result/evidence, proposed changes/questions, cancel, reconcile unknown outcome. |
| P2-S17 Teach mode | Settings → Procedures → Teach a procedure | Example upload/direct correction, proposed procedure, material questions with reasons, tests/replays/shadow/supervised progression, failure blocks promotion, existing permission boundary, rollback with history. |
| P2-S18 Grants/skill versions | Settings → Permissions / Procedures → detail | Exact permission scope/limits/reviewer/effective/expiry/usage receipts/revoke; procedure version history/diff/tests/replays/shadow/promotion/rollback. |

Settings Connections must include Gmail, Exporter Teams, Google Docs translation watcher, Auction discovery, AZKT website, SMS, Calendar, and Accounting/payments. Show what depends on each and last successful sync. Connection maintenance and emergency controls must work when the AI model is unavailable. Procedure detail preserves Proposed → Offline tested → Shadow → Supervised → Bounded automatic, with evidence at each transition.

Correction detail retains the original draft, final sent message, diff, Style/Case fact/Customer exception/Policy proposal classification, explicit scope, and excluded-from-learning state. Teaching and correcting do not grant new permission.

## 5. Necessary completion gaps to design explicitly

These are proposed additions or expansions beyond the supplied screen coverage. They close routine business workflows; they are not claims that the earlier build packet already specified them.

| Gap | Required design surface and boundary |
|---|---|
| Record creation and correction | Contextual create/edit forms; source/evidence attachment; required-field errors; duplicate warning; controlled merge and archive/restore with audit. Preserve original identifiers and conflicting evidence. |
| Inventory acquisition | Inventory candidate intake through Sourcing; investigation/translation/bid where applicable; purchase confirmation; conversion to purchased vehicle with invoice/source and shipment. Also support recording an already-purchased vehicle without pretending the app placed its bid. |
| Reservation, sale, delivery | Vehicle Sale: choose buyer, price/terms, reservation evidence and expiry where applicable, payment reconciliation, document checklist, handoff appointment/evidence, channel updates, title follow-up, aftercare. Design cancellation/refund paths with explicit approvals. Do not invent contractual thresholds or required legal forms. |
| Business-wide money work | Owner Finance: Needs reconciliation, Receivables, Payables, Vehicle costs, transaction detail/source. Include deposit allocation, partial payments, unmatched transactions, refunds, external transaction IDs, typed estimates/quotes/invoices/paid values, and link/export to the accounting system. No payroll, tax engine, or full general ledger in this brief. |
| Cases and promises across records | Tasks secondary views show active cases and commitments, linked to customer/vehicle/request. Include create, assign, waiting/next check, resolve/reopen, and source. One underlying record can appear in multiple scoped views. |
| Scheduling | Tasks → Schedule: pickups, deliveries, shop work, promise/follow-up dates; links to existing Calendar connection and conflict resolution. No separate top-level calendar required. |
| Recovery | Settings → Recovery: failed work, expired access, unknown external result, verify-at-provider workflow, safe retry/cancel, permission denial, and manual fallback. Surface urgent cases on Home with their next action. |
| Access and setup | Owner connection setup, invite/manage employees and roles, insufficient-permission screen, session expiry, lost-access/recovery route through the chosen authentication provider. Design roles without exposing owner data. |
| Evidence reliability | Upload preview/classification/source, progress/retry, checksum verification state, duplicates and replacement history. Mobile interrupted/offline uploads remain Draft or Waiting to upload until saved. Broader offline editing is an open implementation choice. |

Do not add a customer portal, marketing site, supplier portal, social content studio, agent directory, or analytics suite as part of this overhaul. The internal dashboard can manage its existing website/publication integrations without designing those separate products.

## 6. Visual and interaction system

- **Surfaces:** warm off-white canvas, white content surfaces, subtle neutral sidebar, dark charcoal text. Use whitespace and separators before adding containers. Avoid nested cards for ordinary metadata.
- **Accent:** retain AZKT amber as a restrained brand detail. Define an accessible action color with tested text contrast. Give warning, blocked, and destructive states distinguishable labels/icons; never rely on color alone.
- **Typography:** one readable sans-serif with Japanese fallback. Aim for 15–16px desktop body text and at least 16px on mobile. Use smaller secondary text sparingly. Use tabular numerals for currencies, deadlines, and identifiers.
- **Spacing:** shared 4/8px rhythm; approximately 24–32px desktop page padding and 16px mobile padding. Favor useful information density over oversized gaps.
- **Shape:** restrained 8–12px corner radii. One standard primary button, secondary button, text action, input, badge, row, dialog, and inspector pattern. Decorative gradients and repeated glass effects are unnecessary for this direction.
- **Photos:** authentic vehicle photography when available; consistent thumbnail crops. Missing images use “No photo yet” plus Add/request photos. No mascot in an inventory photo slot. A small mascot may appear only in allowed low-stakes empty states.
- **Status:** neutral stage labels; semantic color for actual attention/health. Use “Why blocked?” or “What’s missing?” as useful disclosure controls. Avoid simultaneous badges that repeat the same information.
- **Action hierarchy:** one primary action per page or focused task. Keep common secondary actions visible when useful; put infrequent ones in overflow. Destructive, backward-stage, and override actions require a clear reason/confirmation pattern.
- **Responsive behavior:** verify 1440, 1280, 1024, and 390px layouts, plus narrow/zoomed use. No essential control may be clipped. Board columns remain in a deliberate order; list is the mobile default. All stage changes have a button equivalent to dragging.
- **Accessibility:** keyboard operation, visible focus, WCAG AA contrast, labels for icons/statuses, reduced motion, and at least 44px primary touch targets. Restore focus when overlays close. All required content remains available at zoom and through assistive navigation.
- **Feedback:** distinguish Draft, Saving/uploading, Saved, Awaiting approval, In progress, Failed, and Unknown result. Preserve input on errors. Undo applies only to reversible local changes; cancelling an action does not undo an external action already completed.

### Plain-language replacements

| Current label | Suggested everyday label |
|---|---|
| Now | Home |
| Work | Direct destination names |
| Recon dimension | Shop view |
| Commercial | Sales |
| System | Settings |
| History | Activity |
| Standing grant | Standing permission, with exact scope in detail |
| Skill / Teach mode | Procedure / Teach a procedure |
| Payload changed | Details changed — review again |
| Needs reconciliation | Result unknown — verify, or Payment needs matching, according to the actual issue |
| Run | Automation activity in routine links; Run retained in technical detail |

## 7. Business rules the redesign must preserve

1. Every active case/task has an owner, next action, and sourced due/next-check time, or explicitly No due time set. Do not replace uncertainty with an invented deadline.
2. Reported, Inferred, Confirmed, Conflicted, and Outdated facts remain distinct. Important facts reveal source, observed time, effective time where relevant, and change history.
3. Estimated, Quoted, Invoiced, and Paid money remain distinct and are not double-counted as additive versions of the same expense. Customer-reported payment is evidence to reconcile. Show currencies and dated exchange-rate sources.
4. Requested, accepted, completed, and verified work remain distinct. Parts retain Ordered → Arrived → Installed → Verified. Silence or a failed upload never marks work complete.
5. Stage changes use evidence gates. Unmet requirements create linked tasks while the record stays in its existing stage. Owner overrides and backward moves require reasons and remain visible in activity. A stage override does not change payment facts, grant permission, or authorize an external action. Resolve which operational gates can be overridden in the policy matrix. A buyer’s mandatory requirement cannot be bypassed with the generic override; changing it uses a separate authorized, versioned requirements/agreement flow.
6. Approvals bind to one exact payload version and expiry; edits invalidate the old version. Consequential classes remain individually reviewed. Forwarding a quote and booking a carrier are separate approvals.
7. External actions produce an actual receipt or an explicit unknown-result state. Reconcile unknown results before retrying so a timeout does not create duplicate messages, bids, bookings, or payments.
8. The sourcing chain, logistics legs, listing package/channel publications, and case/task/run/action/commitment/approval records stay distinct underneath the simpler UI.
9. Mandatory buyer requirements cannot be relaxed by AI. Other buyers’ identities and private terms do not leak through shared candidate views or generated messages.
10. A sale does not close unresolved title, delivery, aftercare, or channel cleanup work. Incompatible queued “still available” replies are cancelled and revalidated as appropriate.
11. Permission restrictions apply to the entire data path. Human takeover, opt-out, dispute suppression, and customer-specific exceptions have visible effects and retain their scope.
12. Keep truthful freshness, emergency pause, cancel/revoke, and inspectable audit history. Procedure learning cannot broaden authority. Pausing automation does not reverse completed external actions.

## 8. Required prototype journeys

Retain F1–F9 from the source and demonstrate these complete journeys using consistent fictional records:

1. **Morning triage and approval (F2/F5):** Home → urgent review → edit → details changed/re-review → approve displayed version → pending execution → receipt or failure/unknown → Activity. Also demonstrate expired Gmail and successful refresh recovery.
2. **Import request intake:** Inbox or New request → match/create contact → Must have/Prefer/Avoid → agreement version → deposit evidence/reconciliation → Active search gate. Missing deposit evidence blocks advancement.
3. **Candidate to purchase (F8):** request or inventory sourcing → candidate → unknown/mandatory fail → investigation/translation → complete translation → exact bid packet → approval → Won creates/links vehicle and fulfillment. Request-linked sourcing includes the scoped buyer draft and applicable customer authority; inventory acquisition uses owner authority without requiring a fictitious buyer. Lost preserves exclusions and continues search. A corrected translation invalidates dependent stale work.
4. **Shipment to receipt:** vehicle → shipment → quote comparison with uncertainties → forward approval → separate booking approval → appointment/conflict resolution → pickup/arrival evidence → Received. Show waiting with a durable next check and resume without duplication.
5. **Shop completion and readiness (F1/F4):** employee My tasks → evidence capture → saved completion/required verification → owner sees update. A Ready for sale gate with missing photos creates a task and leaves the vehicle’s stage unchanged. Include blocked and upload-failure paths.
6. **Contextual Ask (F3):** vehicle → “this one needs tires” → proposed change → Apply → recon issue. Repeat without a selected vehicle and show disambiguation. Show two drafts followed by “send it” and require selection.
7. **Customer reply and takeover (F6/F7):** three-question email → one unanswered check → fix/revalidate → exact approval/send → receipt and commitment. Take over → manual reply → resume with revalidation. Include opted-out SMS and unmatched inbound.
8. **Listing to sale and aftercare (F9):** canonical package → en-route/ready gate → per-channel publishing → partial failure/human task → reservation/sale with payment/document checks → sold cleanup and cancelled availability reply → delivery/title/aftercare remain open as needed.
9. **Owner money review:** reported payment → unmatched/partial transaction → inspect provider/source → match to customer/vehicle → confirmed paid state. Show an estimated cost separately from the invoice and settled payment.
10. **Control and recovery:** failed automation/unknown external result → inspect evidence → verify at provider → safe resolution. Teach a procedure → failed replay blocks promotion → corrected version → reviewed progression; inspect/revoke permission and roll back a procedure. Include model-unavailable controls.

## 9. Deliverables and acceptance

Deliver:

1. Sitemap and permissions matrix covering every page, submenu, record detail, and modal above. Mark retained capability, changed presentation, and proposed addition.
2. Reusable component library and design tokens, with examples of compact rows, focused reviews, facts/sources, upload, gates, error/recovery, and permission-limited states.
3. Initial direction set: Home, Vehicles list/Shop board, vehicle detail, Inbox/reply review, and mobile employee task completion. These should demonstrate the intended simplicity before applying the system across every screen.
4. Complete desktop/mobile design set covering all 18 original screen groups and the proposed completion gaps. Pages may consolidate, but no capability may disappear from the matrix.
5. Clickable journeys above, with every visible control either functioning or explicitly disabled with an understandable reason. Prototype-only demo shortcuts sit outside the app frame.
6. A state matrix: empty, loading, error, stale, permission-limited, saved/unsaved, upload failure, blocked gate, invalidated/expired approval, partial publication, unknown result, and record changed while reviewing.
7. A short decision log for unresolved business policy and implementation choices. Keep unspecified deposit amounts, sale/document gates and override boundaries, mandatory-Unknown bidding rules, exact connection freshness thresholds, accounting authority, marketplace support, and offline scope explicit; do not fabricate policies. Resolve the demo-data conflict by using at least six fictional requests to populate six request stages, or explicitly showing empty stages; the source provides only four requests.

Acceptance targets are proposed usability checks, not claims already measured:

- In a short owner walkthrough, the person can identify the next decision, the most important blocker, and its owner within roughly 20 seconds of opening Home.
- A routine record is reachable from a clear destination or search; existing view/filter/scroll state returns on Back.
- The employee can complete an evidence-required task on mobile without horizontal navigation, seeing money, or learning an AI role name.
- Every material action makes the target, consequence, current authority, and eventual result understandable.
- Busy realistic records remain readable, including long names, two similar vehicles, multiple candidates, partial payments, stale facts, and open post-sale work.
- Source F1–F9 all remain demonstrable, including the restored F4 employee flow. Every S1–S18 group has a coverage entry and a reachable equivalent.

The original P0/P1/P2 labels are design sequencing, not permission to ship an incomplete operational product. Sourcing, communication, logistics, listings, and recovery are essential to the described business even when their original mockups were later priority. Any narrower operational release needs its manual fallbacks and excluded workflows stated explicitly.
