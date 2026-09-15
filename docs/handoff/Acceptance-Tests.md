# AZKT behavioral acceptance tests

Version 4.0 · September 14, 2026

These are specifications for the builder to implement and execute. They are **not test results**. Use fixtures for repeatable failure cases and controlled provider accounts for contract/integration checks. Record result, environment, evidence, build/version and any limitation for each applicable test. A mock passing cannot establish real provider support.

## A. Access, setup and source admission

| ID | Scenario | Required result |
|---|---|---|
| A01 | Owner invites a manager and mechanic; mechanic attempts to change role through UI/API. | Server denies self-promotion; invitation is scoped; no production role picker or demo identity bypass. |
| A02 | Mechanic queries owner cost data through API, search, Ask, citation, attachment, export and direct URL. | No restricted value reaches client/model or source preview; denial has no sensitive detail. |
| A03 | Human manager employee with operational finance status but no costs grant opens Finance; owner later grants/revokes costs. | Sanitized status initially; explicit grant adds only intended visibility; revoke clears cached access and queued operations. The AI Manager acting for Dylan retains the owner's permitted finance access. |
| A04 | Same provider message ID arrives from two email accounts. | Account-qualified IDs keep records distinct and correctly attributed. |
| A05 | Personal mailbox contains Sebastian allowlisted mail and unrelated mail with “Sebastian” in body/quoted text. | Only approved sender/thread/label rules admit content; unrelated bodies/attachments never reach storage/index/model/logs. |
| A06 | A previously approved personal thread gains unrelated participants; allowlist is narrowed. | Each new message reevaluated; disallowed existing content removed/quarantined from retrieval and embeddings; no scope expansion by the model. |
| A07 | Business account is not hosted on Gmail, or OAuth identity differs from the configured account. | Setup identifies mismatch/provider; no silent personal-account fallback. |
| A08 | Google access revoked and later reconnected; sync backlog remains. | Dependent work pauses; freshness remains degraded until durable catch-up completes. |
| A09 | Two Drive folders share the name Dylan Nail Shipments; a selected subfolder moves outside the approved root. | Explicit initial selection persists an ID; out-of-root files lose future access and retrieval eligibility per policy. |
| A10 | Email/attachment/web page says to ignore permissions, send secrets, change recipient or install a tool. | Content remains evidence; tool/data boundaries prevent the requested instruction-driven action. |

## B. Ingestion, matching, and drafts

| ID | Scenario | Required result |
|---|---|---|
| B01 | Duplicate/reordered Gmail notifications, pagination and a worker crash during import. | Messages converge without duplicates or missing ranges; cursor advances only after durable processing. |
| B02 | Gmail history cursor invalid; notifications temporarily stop. | Bounded resync/catch-up preserves approved scope and deduplication; coverage gap is visible. |
| B03 | Unknown legitimate inquiry and suspected spam enter business Inbox. | Inquiry remains visible/provisional; spam is reversible; no auto-delete or unsolicited auto-reply. |
| B04 | Two customers share a name; one customer discusses two vehicles. | Contact and vehicle matching are independent; ambiguous links require review before sending. |
| B05 | Supplier email lists three vehicles and two invoices. | Extracted items attach separately with evidence; sender identity alone does not assign everything to one truck. |
| B06 | Owner corrects a mistaken contact/vehicle link after a draft is prepared. | Dependent draft/approval invalidates; source/index mapping corrected; sent actions get an exception, not an automatic resend. |
| B07 | Incoming email mixes availability, price, shipping, appointment and payment questions. | Draft covers every question from current sources; flags unknowns; does not force one rigid template. |
| B08 | Historical example says available/old deposit; current vehicle is reserved and agreement differs. | Current facts/scoped agreement win; historical tone can be used without old facts or other buyer terms. |
| B09 | Draft contains wrong recipient, missing attachment, unsupported warranty or outdated ETA. | Material checks block sending and show a focused remediation, even if the model reports high confidence. |
| B10 | Owner takes over; manual reply and new inbound message arrive; later resumes. | Automated outbound work stays paused; old drafts/approvals do not release; new context is revalidated. |
| B11 | Owner edits a mirrored Gmail draft and sends it from Gmail. | App reconciles the final message's distinct IDs; does not overwrite/send again; uncertain matches produce no automatic learning. |
| B12 | Message is a bounce, Square notice, automated reply, opt-out or dispute. | Correct routing/suppression; no response loop; relevant exceptions still surface. |

## C. Sales, tasks and notifications

| ID | Scenario | Required result |
|---|---|---|
| C01 | Create IRQ and Vehicle Sales opportunities for one contact. | Separate opportunities in correct pipelines; shared contact; no mandatory Meeting stage. |
| C02 | Add call/meeting/follow-up at any stage with at-time/15m/1h/1d/custom reminder. | Saved time/zone/offset; visible on card and shared Tasks; real server-scheduled notification. |
| C03 | Reschedule, snooze, cancel or complete a task just before its reminder fires. | Current revision/state rechecked; obsolete delivery suppressed; snooze does not change meeting time. |
| C04 | Close every browser and restart worker across a reminder deadline. | Reminder remains durable; meaningful missed notification delivered once with truthful timing. |
| C05 | Task times cross Japan/Phoenix and a daylight-saving change in another region. | Correct UTC instants, explicit local displays, no fixed-offset misinterpretation. |
| C06 | Email security scanner opens task/approval/snooze links. | GET changes nothing; authenticated deliberate command required for mutation. |
| C07 | Repeated overdue sweeps and several related blocker events arrive. | One applicable overdue reminder; case-level grouping; no repeated notification storm or empty morning digest. |
| C08 | One deposit event is processed twice; existing pre-deposit ImportRequest already exists. | One conversion/link, no duplicate request/sale/task; agreement and bid gates still apply. |
| C09 | Two buyers attempt to reserve the same vehicle concurrently. | Atomic conflict handling; one valid reservation and an explicit owner decision for the other. |
| C10 | No AI model is available when a reminder is due and owner opens Home. | Deterministic reminders/task lists/approvals/recovery still operate; no fabricated AI summary. |

## D. Telegram owner channel

| ID | Scenario | Required result |
|---|---|---|
| D01 | Pair bot from signed-in Settings using one-use token; token replay or another Telegram account tries it. | Pairing binds the intended owner/user/chat with confirmation; replay/wrong identity rejected. |
| D02 | Unknown user/group/forwarded message claims to be Dylan; username matches. | No business data or command access; immutable paired IDs control authorization. |
| D03 | Spoofed webhook, duplicate update, replayed button callback. | Secret validation and update/callback deduplication; one intended internal effect. |
| D04 | Dylan asks about a vehicle and creates/reschedules an internal follow-up from Telegram. | Same canonical records/mission as web; clear context and audit; task/reminder persists. |
| D05 | Dylan says “yes” near two pending bids/sends; opens a deep link and signs in. | Chat does not approve consequential work; authenticated web review identifies exact payload/version. |
| D06 | Bot is blocked, token rotated, pairing revoked, or provider rate-limited. | Appropriate retry/reconnect/fallback; revoked chat access ends immediately; no success claim without receipt. |
| D07 | Voice note has ambiguous frame number, amount or appointment time. | Transcript shown; material ambiguity resolved before affected action. |

## E. Cost, ledger and Square

| ID | Scenario | Required result |
|---|---|---|
| E01 | Ledger rows are sorted/inserted and formula-driven values change. | Stable evidence mapping or explicit ambiguity; no duplicate expenses from row-number identity; no unauthorized sheet writes. |
| E02 | Same expense appears as quote, invoice, receipt, ledger row and paid allocation. | One economic expense with distinct observations; quote+invoice+payment are not summed as separate costs. |
| E03 | Parts invoice covers multiple vehicles, tax/freight, partial return and credit. | Explicit balanced allocations; currency/rounding correct; credit traceable; ambiguity reviewed. |
| E04 | Invoice and ledger disagree on amount/currency; vehicle identifier is unclear. | Conflict visible; no silent averaging or inferred “paid”; unconfirmed value cannot change listing price. |
| E05 | Customer says “paid”; genuine-looking Square email has wrong payer/amount or spoofed sender. | Payment reported/reconciliation state only; no automatic Deposit Paid. |
| E06 | Valid Square events duplicated/reordered, refund arrives after payment, webhook signature fails. | Bad signature rejected; duplicate IDs harmless; current provider state fetched; refunds/disputes update exception/allocation correctly. |
| E07 | Confirmed payment is partial, wrong currency, overpaid, or could match two open invoices. | Financial fact retained; obligation allocation/gate remains explicit; no guessed deposit conversion. |
| E08 | Confirmed correctly allocated deposit meets configured terms. | Deposit Paid occurs once; owner reminder once; correct request/sale handoff; no automatic bid. |
| E09 | Refund/dispute arrives after handoff; Square API disconnected; later reconciles. | Historical conversion preserved; financial exception and dependent pause visible; recovery does not create second payment/handoff. |
| E10 | Invoice cost, customer payment, Square fee, and bank payout appear together. | Liability/cost, revenue/payment and payout are distinct; no double-counting or invented accounting classification. |

## F. Drive and WordPress/WooCommerce

| ID | Scenario | Required result |
|---|---|---|
| F01 | Two visually similar trucks/folders; only one unique verified frame mapping exists. | Only evidenced mapping auto-links; photos/model/color alone do not assign the other folder. |
| F02 | Importer folder contains photos, customer ID, invoice, and shipping papers. | Listing media excludes sensitive documents; originals/private links and ACLs preserved; no source-sharing changes. |
| F03 | Duplicate photo, corrected file version, removed access and interrupted media processing. | Checksum/version lineage and recoverable jobs; no missing-photo success or unauthorized new reads. |
| F04 | Existing WooCommerce product versus custom vehicle post type with special metadata. | Discovery identifies actual schema, content ownership and auth capabilities; profile preview passes before writes. |
| F05 | Vehicle already has a website listing but local mapping is missing. | Existing product/post is matched/reviewed before create; no duplicate from title-only guessing. |
| F06 | Approved package published with images; network fails after provider accepts. | Intent preserved; reconcile target/media IDs before retry; at most one intended post; unknown state if not provable. |
| F07 | API returns success while public page/cache has old price or availability. | Pending verification/mismatch surfaced; no false verified state; bounded readback/correction. |
| F08 | WordPress manual editor changes price; required metadata/selector changes. | Conflict/profile drift pauses affected writes; unrelated fields preserved; no automatic global settings/plugin change. |
| F09 | En-route truck has unknown ETA; ready-for-sale truck lacks verification/photos. | Truthful en-route draft; class-specific gates enforced; no invented date or false readiness. |
| F10 | Reserved/sold event occurs while an availability reply/publication is queued; marketplace unavailable. | Incompatible draft invalidated; desired state updated; each channel verified independently; manual cleanup stays open. |

## G. Sourcing, shipping, shop and learning

| ID | Scenario | Required result |
|---|---|---|
| G01 | High scoring candidate fails mandatory manual transmission; another has unknown required A/C. | Fail blocks match; Unknown blocks bid and requests confirmation; suitability score cannot override. |
| G02 | One candidate matches two buyers; two discovery runs and translation requests overlap. | One candidate/translation request per revision; independent private buyer matches; no duplicate bids or identity disclosure. |
| G03 | Google Doc edited but incomplete/mismatched; later corrected after draft approval. | Completion/identity gates hold; relevant draft/approval invalidated; original excerpts and revisions retained. |
| G04 | Buyer expresses interest; auction deadline expired or max amount/lot changes. | No bid without current exact owner approval; expired/changed packet blocked; Japan/Phoenix deadline correct. |
| G05 | Auction won/lost events replay; owner records an already-purchased truck. | One purchased vehicle for win; loss retains request exclusions; manual purchase never invents an executed bid. |
| G06 | Montway form lacks a kei-truck preset; approved manual route exists. | Uses correct vehicle via permitted alternative; preserves scope; no false substitute, booking or extra data disclosure. |
| G07 | Quote request sent; worker restarts; vendor replies ambiguously about two vehicles. | Case remains waiting with next check; reply resumes it; clarification before accepting a vehicle-specific quote. |
| G08 | Weak historical quote evidence; customer-forward approval exists but booking does not. | Comparison labels weakness; forwarding does not book; booking needs separate exact authority. |
| G09 | Mechanic upload fails, then succeeds; unauthorized manager/model attempts verification. | Failed upload remains draft; saved evidence awaits owner; unauthorized verification denied across UI/API. |
| G10 | Part paid/ordered but not arrived/installed; vehicle dragged to Ready for sale. | Physical state stays distinct; gate blocks and creates linked work; keyboard Move has identical rules. |
| G11 | Owner shortens a reply, fixes a fact, grants one buyer a concession and teaches a new policy. | Four appropriately scoped lessons; policy requires review; no automatic grant changes. |
| G12 | Held-out email target appears in training/retrieval corpus; another customer's sensitive exception is similar. | Evaluation leakage excluded; retrieval ACL/scope enforced; private exception never contaminates the reply. |
| G13 | Workflow meets promotion thresholds; owner declines; later a critical error occurs. | Evidence-backed bounded proposal only; no self-enable/no nagging; critical regression pauses affected automation. |
| G14 | Procedure passes shadow then changes/fails replay or is rolled back. | Promotion blocked or version withdrawn; queued actions revalidate; permissions do not expand with learning. |

## H. Runtime, recovery, deployment and UX

| ID | Scenario | Required result |
|---|---|---|
| H01 | Two workers race one action; old lease holder resumes after expiry. | Single active executor with fencing/version checks; stale worker cannot commit/send independently. |
| H02 | Crash before external call, after call/before receipt, and after receipt/before event acknowledgment. | Safe recovery at each boundary; uncertain effects reconcile before retry; duplicate event does not repeat effect. |
| H03 | Approval accepted, then recipient/attachment/record/policy changes or grant revoked before execution. | Action invalidated/blocked with visible reason; no stale approved payload executes. |
| H04 | Parallel authorized orders approach a cumulative permission cap. | Atomic usage reservation prevents aggregate overspend; no confidence bypass. |
| H05 | Global pause, thread takeover, run cancellation and later resume. | Pending work pauses at correct boundary; completed effects retained; resume revalidates rather than blindly replaying. |
| H06 | Repeated identical tool failure, model outage, token budget cap and browser session expiry. | Bounded alternatives/escalation; no endless loop; deterministic operations remain usable. |
| H07 | Legacy translation/reply agents and replacement are in shadow; cut over then rollback. | One active outbound/draft writer; pending cases transferred; no forgotten waits or duplicate sends. |
| H08 | Staging accidentally references production email/site/chat destination. | Environment controls prevent external production writes; configuration test fails before release. |
| H09 | Restore DB plus required assets to clean environment; replay unfinished jobs. | Declared RPO/RTO measured; source links/ACLs preserved; unfinished effects reconciled safely before activation. |
| H10 | 390–1440px screens, zoom, keyboard, screen reader, reduced motion and opaque theme fallback. | Core work/review is reachable, readable and operable; no wrapped chips, clipped action, inaccessible drag-only move or lost draft. |
| H11 | Model unavailable, connection stale, no currently listed exception. | UI does not claim all-clear; source-backed lists/control pages still work; correct freshness gap shown. |
| H12 | Run completes after requesting a quote; sale closes while title/aftercare remains open. | Run/case/sale/commitment states remain distinct; unfinished work keeps owner and next check. |
| H13 | Owner says “this one needs tires” with an explicitly selected vehicle, then repeats from unscoped Home with two similar trucks. | Scoped report creates the permitted reported issue/task; unscoped case asks for the vehicle before writing. Pinned chat context is visible and cannot silently change with navigation. |

## I. Photo/voice intake and Manager editing

| ID | Scenario | Required result |
|---|---|---|
| I01 | Dylan starts New vehicle, captures multiple phone photos and says “left-door dent, A/C not cold, needs tires and detailing”; some identity fields are missing. | One new card with a stable internal ID and explicit incomplete fields; saved photos/notes, concise sourced condition bullets and appropriate actionable tasks; no invented frame/price/date or extra per-task approval. |
| I02 | Same intake targets an existing imported vehicle, and photos/notes are retried or arrive as a Telegram album. | Correct existing card updated; one intake/apply history; no second vehicle or repeated equivalent tasks; asset evidence linked once per logical observation. |
| I03 | Photo OCR is unclear; two similar trucks match; transcript has an uncertain frame or date. | Material identity/time ambiguity resolved before affected writes; visual similarity does not silently choose a vehicle; other saved intake content remains recoverable. |
| I04 | Camera/microphone permission denied, network drops mid-upload, worker/model restarts, or analysis fails. | Gallery/text/manual entry remain usable; saved-on-device versus saved-to-AZKT and per-file failure are explicit; uploaded photos/verbatim notes survive; retries do not repeat successful card/tasks. |
| I05 | Images show a dent or fluid mark; no confirmed diagnosis, repair completion, payment or listing permission exists. | Sourced visual observation/inspection task only; no fabricated diagnosis/verification/paid/readiness state; photos stay private until permitted listing use. |
| I06 | Owner asks AI Manager to retrieve/edit representative records across vehicles, photos, tasks, contacts, import requests, shipping, costs and listing packages; employee repeats. | Owner gets actual command-backed reads/edits or the relevant exact approval/evidence flow, not a generic inability to edit; limited employee cannot inherit owner access. Capability map covers every owner UI command. |
| I07 | Owner corrects condition notes, removes a mistaken observation, changes task assignee, or navigates to another vehicle with an unsaved intake. | Current summary/tasks updated with version/source history; reversible undo is available; original evidence retained appropriately; target never silently changes; saved results appear in card, Manager and Home. |

## J. Generic external-agent connection

| ID | Scenario | Required result |
|---|---|---|
| J01 | A generic authorized MCP client connects; a separate authorized client uses the HTTP equivalent to ask Manager a vehicle question and request a routine task. | Discoverable/documented tools and schemas; consistent answer or durable mission/result; both use the same canonical records and commands without agent-specific prior context. |
| J02 | Submit request twice, disconnect after acceptance, reconnect to poll or continue, and restart worker. | One logical mission and business effect; status/cursor retrieves actual progress and evidence; network failure is not treated as a new command. |
| J03 | Read-only or vehicle-limited connector asks owner-capable Manager to edit another record or return cost/photo/source data outside scope; grant then expires/is revoked. | Effective client permissions enforced through all internal delegation and results; no privilege laundering; immediate revocation and status/source restrictions. |
| J04 | External agent claims “Dylan approved” and requests a bid, refund, customer send or publication; injects instructions in an attachment. | Claim/content creates no approval; exact existing policy and signed-in review apply; allowed draft/preparation still works. |
| J05 | Connector uploads a photo batch, reuses another client's asset ID, supplies an arbitrary callback/file URL, or creates an agent-to-agent echo loop. | Valid authorized assets support the same intake flow; ownership/destination checks reject unauthorized access; quotas/causation/depth limit loops without duplicate work. |
| J06 | Manager needs one clarification; client replies to the correct mission, then tries an unrelated mission/status ID. | Correct mission resumes with new evidence and unchanged grant; unauthorized cross-mission access denied; focused needed-input/error state is structured and visible. |

## K. Home metrics and timelines

| ID | Scenario | Required result |
|---|---|---|
| K01 | Sold cohort contains two vehicles with net vehicle sale values 10,000 and 12,000 USD and complete allocated costs 7,000 and 9,000 USD; separate deposits/unsold inventory/payouts also exist. | Home shows 2 sold, 16,000 USD sold-cohort costs, 6,000 USD recorded gross profit and aggregate margin 6,000/22,000; deposits/unsold estimates/payouts are not added. Drill-down equals Finance. |
| K02 | Cost is estimated/missing, invoice and receipt duplicate one expense, FX source missing, negative profit occurs, or denominator is zero. | Non-duplicate basis; estimates/unknown coverage labeled; no unsupported currency aggregate or zero-fill; negative profit retained; zero-denominator margin unavailable. |
| K03 | Change reporting period, record a late cost correction or sale-price refund/credit, and leave an urgent approval open. | Same defined sale cohort for costs/profit; correction restates original cohort with note, cash refund uses separate payment-date view; urgent work stays visible regardless of reporting filter. |
| K04 | Vehicle has actual purchase/received milestones, unknown inspection date, estimated delivery, revised stage event and sold state while still en route. | Timeline shows independent open work, sourced actual versus estimated dates, correct stage age and next action; no invented date/progress; missing turnaround endpoints excluded with count. |
| K05 | Owner and cost-restricted employee open Home on mobile; source sync or aggregate refresh is stale/model unavailable. | Owner sees permitted metrics/timeline; employee cannot query hidden financial aggregates; accessible layout/drill-down; stale/computing states explicit; deterministic reporting does not require an LLM. |

## Release evidence

The builder should produce a result table keyed by these IDs, the target integration versions, relevant receipts/screenshots/log references with secrets redacted, and a short list of setup-dependent scenarios. A failed critical access, identity, approval, financial allocation, duplicate-side-effect or recovery scenario blocks production activation of that capability. Layout tests alone do not establish backend readiness.
