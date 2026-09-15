# AZKT decisions and connection setup

Version 4.0 · September 14, 2026 · Companion to AZKT-Full-Build-Spec.md

## Confirmed by Dylan in this handoff

| Item | Decision |
|---|---|
| Business email | **info@azkeitrucks.com**. Verify actual hosting provider during connection setup. |
| Website | **WordPress / WooCommerce**. Preserve the existing site's structure and settings; connect the management app to it. |
| Ledger | In **dylxnxil Google Drive**. Select actual sheet ID and map columns; read-only initially. |
| Importer folder | **Dylan Nail Shipments**, in that Drive. Persist folder ID after resolving any duplicate names. |
| Day-one autonomy | Automate internal work and reminders; draft emails; Dylan approves customer sends and publication. |
| Growing autonomy | Build evidence of reliability and ask Dylan when a specific workflow is ready. Only Dylan enables its bounded permission. |
| Outside-app communication | **Telegram** for private Manager chat and reminders. Keep originally requested email reminders. |
| Hosting | AZKT services on **Railway**; external Google/Square/Telegram/model APIs and existing WordPress host remain external. |
| Vehicle intake | Camera/gallery photos plus voice/text condition notes through Manager; create a new card or update the matched existing one with condition bullets, evidence and tasks. |
| Manager coverage | AI Manager acting for Dylan can access and edit all owner-editable business records, including costs/profit and timelines, through shared commands and existing consequential-action gates. |
| External agent connection | Generic authenticated connector into Manager; MCP plus equivalent HTTP interface. No personal agent-specific implementation or terminology. |
| Home | Display timeline, profit, costs and related sales/turnaround metrics directly on Home, with source detail and estimate/completeness labels. |

## Confirmed by Dylan, September 15 2026

| Item | Decision |
|---|---|
| Business SMS | **Dropped.** Not built and not planned. The dedicated number, shared SMS inbox, consent/opt-out handling and delivery receipts described in spec §5.5 are out of scope until Dylan says otherwise. Customer contact stays email, Telegram (private) and the phone he already answers. |
| Marketplaces | **Deferred, manual.** Facebook Marketplace and similar have no workable listing API, so no adapter is built. Publishing to any channel other than the website stays the existing path: AZKT generates the copy, photos and checklist and assigns posting **and cleanup** to a person, tracked per channel (spec §7.3). |
| Calendar | **Google Calendar on `info@azkeitrucks.com`.** Appointments only — calls, meetings and scheduled blocks become events. Operational to-do tasks do not. AZKT stays the authority for reminders; the calendar is a mirror people can see. |
| Website media | Listing photos are **uploaded into the site's own media library** and referenced by media id. The site cannot fetch an AZKT asset URL, so a product written with URLs publishes with no images. |
| Website SKU | The live shop numbers its own products, so the AZKT stock number is **not** the site SKU. AZKT leaves that column alone (`sku_strategy = preserve`) and identifies its listings by the `azkt_vehicle_id` meta. A vehicle is bound to an existing product by a person confirming it, never by a guess. |
| External agent | Dylan's personal agent is the one external client. Callbacks are delivered to the destination **he** configures in Settings; a URL that appears in a request or in model output is never called. |

## Handoff implementation decisions

These resolve incomplete or conflicting design notes. They are explicit builder defaults, not claims that Dylan supplied each technical value.

- Latest Liquid Glass II HTML supplies the visual direction; older no-glass/Instrument Sans notes are historical. Preserve workflow simplicity and accessible opaque fallbacks.
- One runtime; Manager plus Customer & Sales, Sourcing & IRQ, Logistics, Shop & Fulfillment, Listings, Finance & Documents logical roles.
- TypeScript domain code, Postgres records/jobs/vector retrieval and maintained durable queue; adapt to an existing sound repository instead of gratuitous rewrites.
- Separate role access, action permission and evidence confidence. Remove the global auto-send bypass.
- Owner-controlled work verification and team administration. **Human manager employee** finance status is sanitized until costs are explicitly granted. The **AI Manager acting for Dylan** uses owner business access and is not limited by that employee preset. Mechanic requests parts; purchasing is separate.
- Square email is a signal; authenticated provider or authorized manual evidence confirms payment. No hardcoded deposit or spending limit.
- Exact owner review for customer sends and publication at launch. Vendor quote/translation requests need exact mission/action authority initially too.
- Candidate Mandatory Unknown blocks bidding; it may still justify translation/verification. Buyer must-have changes need an authorized versioned requirement change.
- Email/Telegram links open the signed-in review for consequential actions. GET requests never approve or complete work.
- PWA installability is useful; web push is optional later because Dylan chose Telegram.
- Owner intake saves routine internal card/condition/task changes directly when identity and intent are clear. Photos do not by themselves establish mechanical readiness or paid state; ambiguous vehicle identity is resolved before linking.
- Home profit uses the same sold-vehicle cohort/cost basis as Finance, distinguishes estimates and recorded gross profit, and treats unsold projected profit separately. Default period is this month in Phoenix time; open urgent work stays visible regardless of historical filters.
- External-agent access follows a per-client grant through Manager, including results and source retrieval; the owner-capable Manager cannot elevate a restricted connector. Keep MCP/HTTP tool names and setup generic.

## Setup values to collect, without guessing

| ID | Needed value | Why / scope held until available | Useful work that can proceed |
|---|---|---|---|
| S01 | Confirm full personal Google email, expected `dylxnxil@gmail.com` but not yet confirmed | Correct OAuth identity/account association | Build Google adapter and selector UI. |
| S02 | Exact Sebastian/port senders, approved domains/threads/labels | Personal-mail admission; no name-based broad scan | Business email and other source adapters. |
| S03 | Actual ledger sheet ID, tabs, existing unique IDs, column meanings and currencies | Correct import/match and source authority | Read-only mapping preview, fixtures, cost model. |
| S04 | Chosen Dylan Nail Shipments folder ID and importer naming examples | Correct file coverage and vehicle matches | Folder selector and mapping workflow. |
| S05 | Website base URL, staging URL, installed vehicle/product type and metadata/plugins | Exact adapter mapping and safe pilot | Woo/WordPress adapters, local listing preview. |
| S06 | Dedicated WordPress/WooCommerce integration access with appropriate capabilities | Read existing records; later approved product/media writes | Discovery and contract tests with fixtures/staging. |
| S07 | Square merchant/location connection and relevant payment/invoice flow | Authoritative automatic reconciliation | Email signals and owner evidence-based manual matching. |
| S08 | Agreement/deposit rules per IRQ and vehicle sale; required documents; reservation expiry terms | Active-search/reservation/paid gates | Intake, drafting, tasks, sourcing research and cost views. |
| S09 | Team identities, scopes, manager relationships, any cost/parts grants | Actual staff access | Role defaults and invitation flow; no fictitious accounts. |
| S10 | Verified owner reminder email, Telegram bot and owner pairing, quiet hours/digest time | Production notification delivery | Templates, durable schedule, test bot/recipient. |
| S11 | Exporter Teams account/chat and supported integration route; Google translation document IDs/completion convention | Correct translation requesting and completion checks | Candidate matching and manual translation-request task fallback. |
| S12 | Existing auction source, schedules, credentials by secure connection; agent/corpus exports and pending work | Continuity of matcher/watcher and migration ownership | Runtime/domain scaffolding and new intake. |
| S13 | Approved sending aliases/signature, current policies/style, corpus coverage and sample corrected replies | High-quality reply evaluation and exact outbound account | Draft workflow using scoped fixtures and admitted live context. |
| S14 | Per-run/day/month model and external-service budget; any future spending caps | Recurring model-work activation and bounded purchase permissions | Manual controls, deterministic timers, ingestion and supervised testing. |
| S15 | Google Calendar consent on `info@azkeitrucks.com` (see the decisions above: SMS dropped, marketplaces stay manual) | Writing appointments onto the business calendar | Tasks, reminders and the manual channel package already work without it. |
| S16 | Retention and recovery needs beyond proposed 24h RPO / 4h RTO | Production backup/asset retention configuration | Restore tooling and isolated restore drill. |
| S17 | External client's supported MCP/HTTP authorization, desired record/action scope and owner pairing | Activating that client's access; do not infer it from text claiming owner approval | Build/document generic connector and use a controlled test client. |
| S18 | Existing recorded acquisition/sale dates, cost allocation basis and cost-completeness rules | Labeling cost/profit/timing coverage accurately | Home layout, deterministic formulas and explicit incomplete/estimated states. |

Ask for these through connection selectors and focused setup steps when relevant. Do not ask Dylan for all secret values in chat; use OAuth or the chosen secure configuration UI. No account is connected merely because it is named here. Never hold unrelated development for a missing field.

## Autonomy promotion review

Initial proposed evidence threshold: at least 50 reviewed eligible cases over at least 14 days, at least 95% accepted without substantive correction, zero critical errors, and passing targeted failure cases. Review by workflow, not one global confidence score. These are editable operating defaults and do not prove future safety.

Each proposal must show: exact action class, eligible recipients/records, excluded cases, current performance/sample examples, sources/freshness needed, frequency/amount caps if relevant, expiry, monitoring, pause/revoke, and what will still require review. A general “take over emails” switch is insufficient. Declining keeps supervision; a serious regression pauses the affected automatic workflow.

## Production enablement

Connection setup authorizes the stated data access. Enabling reminders authorizes the configured owner notifications. Exact send/publication approval authorizes that reviewed action. A separate standing-permission review enables ongoing eligible actions. Corpus/procedure learning grants no new authority. Keep these boundaries visible without asking for approval on every harmless read or internal task update.
