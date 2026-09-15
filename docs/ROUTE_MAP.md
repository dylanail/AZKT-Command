# Route map: prototype screens → implemented routes

The Liquid Glass II prototype (docs/handoff/reference/design-v2) and the page contract in the spec (§2.3) name the
screens below. Every record has a stable deep link; list filters live in the URL so Back restores the view. Employees
(mechanic, logistics) get the reduced shell (My tasks · Vehicles · More); the server enforces every permission.

| Prototype / spec screen | Route(s) | Notes |
|---|---|---|
| Sign-in (passkey) | `/login` | WebAuthn passkeys; setup token bootstraps the first owner (`/api/enroll`). No production role picker. |
| Invitation accept | `/invite/:token` | One-time link; the device creates a passkey. Expired/revoked links say so. |
| Expired session / Denied | `/expired`, `/denied` | Denied pages never reveal whether a record exists. |
| Home | `/` (`?period=month|7d|30d|custom&start=&end=&horizon=`) | Status line (H11), Business overview + drill-downs, Needs your decision, Needs attention, Today, Vehicle timeline, In progress, Completed. Employees are redirected to `/tasks`. |
| Vehicles list / board | `/vehicles` (`?view=all|sourcing|shipping|shop|sales&layout=list|board`) | Saved views; Move stage from the keyboard. |
| Vehicle detail | `/vehicles/:id` (`?tab=overview|work|files|sale|money`) | Money tab obeys `costs.read`; Files separates Photos and Documents; Sale tab links the listing package. |
| Photo / voice intake ("Add update") | `/vehicles/intake`, `/vehicles/:id/intake` | New or existing vehicle; required condition note; transcription is optional (typed fallback). |
| Sales (IRQ + Vehicle Sales) | `/sales` (`?pipeline=irq|vehicle&layout=board|list&stage=&lead=`) | Lead detail opens in the inspector on desktop, full page on phones. |
| Lead detail | `/sales?lead=:id` | Contact, inquiry, notes, correspondence, requirements, next task; deposit evidence and fulfilment hand-off. |
| Import requests | `/requests`, `/requests/:id` | Requirement tiers, agreement/deposit, candidates, activity, linked inbox and purchased vehicle. |
| Candidate detail | `/candidates/:id` | Auction identity/snapshot/deadline, per-request Pass/Fail/Unknown checks, translations, scoped customer draft, exact bid packet. |
| Tasks | `/tasks` (`?view=my|all&bucket=upcoming|overdue|unassigned|blocked|waiting&secondary=cases|promises|schedule`) | One task can appear in several views without duplication. |
| Employee task / Task detail | `/tasks/:id` (`?action=done|snooze|reschedule|cancel` from reminder emails → confirm first, never on GET) | Complete task with evidence, I'm blocked, upload progress, recoverable draft, Awaiting verification. |
| Inbox | `/inbox` (`?filter=needs_reply|drafts|taken_over|unmatched|all&account=&q=`), `/inbox/:threadId` | Thread, contextual records, checks, editable reply, Take over / Resume; account and recipients always visible. |
| Contacts | `/contacts`, `/contacts/:id` | Identities, linked records, history, consent, promises, reversible audited merge. |
| Shipments | `/shipments`, `/shipments/:id` | Legs and milestones, release/storage evidence, quotes, separate forwarding/booking decisions, delivery evidence. Logistics role lands here. |
| Listing editor | `/listings/:id` | Versioned package, preview/diff, readiness checks, per-channel publication state; publish only via approval. |
| Approvals queue / Exact approval | `/approvals`, `/approvals/:id` | Same record from Home, Inbox, Ask, notifications and email deep links; approve / edit / decline; invalidated, expired, execution status and receipt. |
| Agents / Ask | `/agents`, `/agents/:agentId` (`?context=vehicle:<id>`) and the Ask inspector on every page | Manager default, specialist roles, persistent web+Telegram thread, attachments, streamed replies, missions/runs, coverage map (owner). |
| Activity | `/activity` (`?kind=&entity=&q=`) | Append-only business events with execution detail, versions, source/receipt, retries. |
| Finance | `/finance` (`?tab=matching|receivables|payables|costs|sold|ledger&period=&from=&to=`) | Needs matching, Receivables, Payables, Vehicle costs, Sold cohort, Ledger mappings; export CSV. `finance.status` roles see states without amounts. |
| Settings → Connections | `/settings/connections` | Gmail (business/personal), Drive, Sheets, Square, WordPress/WooCommerce, Telegram; freshness and coverage. |
| Settings → Team & access / People | `/settings/team` | Owner: invitations (one-time links, reissue, revoke), roles, per-person permission overrides, cost grants. Manager: scoped People page. |
| Settings → Reminders | `/settings/reminders` | Channel per reminder kind, quiet hours, digest, Telegram pairing, recent deliveries. |
| Settings → Automation & permissions | `/settings/automation` | Pause switches, spending caps, standing permissions, stage gates, model budget. |
| Settings → External agents | `/settings/external-agents` | Register / rotate / revoke MCP+HTTP clients with scopes, record limits, quotas; connect URLs and tool catalog. |
| Settings → Procedures / Teach | `/settings/procedures` | Teach, versions, tests, promote / roll back. |
| Settings → Knowledge | `/settings/knowledge` | Corpus manifests, admissions, retrieval check, re-index one source. |
| Settings → Website | `/settings/website` | Site profile (WordPress/WooCommerce mapping), publications and failures. |
| Settings → Drive importer | `/settings/drive` | Importer root folder, scan, matches, import. |
| Settings → Recovery | `/settings/recovery` | System health (owner), passkeys, sign out. |
| Settings → Usage | `/settings/usage` | Model budget and spend (owner). |
| More (mobile) | `/more`, `/more/:section` | Everything not on the tab bar, as permitted. |

## API surface

Human UI: `/api/*` (session cookie). Health: `/healthz`, `/readyz`. External agents: `POST /mcp` (remote MCP,
Streamable HTTP, bearer token) and `/api/integrations/v1/*` (HTTP equivalent; `GET /api/integrations/v1/openapi-lite`
documents it). Provider webhooks: `POST /api/webhooks/gmail`, `POST /api/webhooks/square`, `POST /api/telegram/webhook`.

Route deviations from the spec text: the live ledger sync is `POST /api/finance/ledger/source/sync` (the finance router
owns `/api/finance/ledger/{action}`); Home queue links use `/inbox?filter=unmatched` and `/tasks?secondary=promises`.
