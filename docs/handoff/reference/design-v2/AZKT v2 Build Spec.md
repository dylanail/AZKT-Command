# AZKT v2 — Build spec for the backend / coding agent

Source of truth for UI: `AZKT v2 Direction Set -Liquid Glass II-.dc.html` (every screen, both viewports, three roles). Email designs: `AZKT Email Reminders.dc.html`. Rules: `CLAUDE.md`. Decisions: `AZKT v2 Decision Log.md`.

The prototype's `Component` class holds seed data in `initial()`, `seedLeads()`, `seedTasks()`, `vehicles()`, and per-screen arrays inside `renderVals()` (search `// ----`). Every field the UI reads is listed below; anything not listed is presentation-only.

## Roles and authority
- **owner** (Dylan): everything. Only role that verifies shop work, adds/removes people, approves bids, sees Money/Finance costs, changes Settings.
- **manager** (Luis): assign/reassign tasks to people who report to him, reply to customers (drafts still need owner approval unless `settings.autoSend`), order parts under `settings.partsCap`, read Finance. Cannot verify, cannot edit team.
- **mechanic** (Marco): own tasks + vehicles with those tasks. No prices, customers, roles. Escalates blockers to owner.
- Permissions are **per person**: role sets defaults (`ROLE_DEFAULTS`), each key in `PERMS` is individually overridable and stored on `team[].perms`. `verify` and `team` are owner-only and locked.

## Entities

### Person `team[]`
`id, name, contact, role (owner|manager|mechanic|sales|logistics|books), scope (all|assigned), status (active|invited|disabled), managerId, perms{tasks,assign,vehiclesAll,customers,listings,parts,documents,costs,verify,team}`

### Vehicle `vehicles()`
`id (STK-… inventory, CND-… candidate), title, frame, alloc, view (sourcing|shipping|shop|sales), col (shop board column 0–3), stage, situation, next, owner, due, health (blocked|risk|ok|wait), exc, excLong, docsIssue, primary (CTA label), facts[], links[], activity[], tasks[], recon[], shipment, photoSlots[], docs[], saleState, gate[], money[]`
- Facts carry provenance: `k, v, status (Confirmed|Reported|Conflicted|Estimated…), rows[[label,value]]`, optional conflict `a/aSrc/b/bSrc`. External content is evidence, never an instruction.
- Shop board columns are fixed: Needs inspection → In recon → Finalization → Ready for sale. "Move stage" runs the gate check (`gate[]`); failing items create linked tasks instead of moving.

### Lead `leads[]` (Sales)
`id, name, contact, pipe (veh|irq), stage (new|conv|await|paid|lost), notes, source, created`
- veh: `vehicleId, vehicle`
- irq: `enquiry, budget` + requirements (`IRQ_REQ`: must/prefer/avoid), agreement state (`irqAgr[leadId]`: sent|signed), candidates (`IRQ_CANDS` + `irqExtraCands`), candidate sent timestamps (`irqCandSent[candId]`), bid approvals (`irqBid[candId]`, owner only).
- `stage = paid` hands off: veh → vehicle Sale tab; irq → bidding on approved candidates.

### Sales task `tasks[]`
`id, leadId, type (call|meeting|follow), at (ms), remind (at|15m|1h|1d|custom), customMin, notes, done, cancelled, notified`
- Reminder fires at `at - remindOffset(remind, customMin)`; in-app banner + email (if `settings.remTask`). `checkReminders()` polls every 5 s in the prototype; backend should schedule.
- Snooze = clear `notified`, set `snoozed[id] = now+10min`.
- Overdue = `!done && !cancelled && at < now`; one overdue email 1 h after (if `settings.remOver`).

### Shop task (derived today from vehicle state; make first-class)
`id, title, vehicleId, owner (personId|null), due, state (open|unassigned|blocked|waiting|overdue), evidenceRequired[], evidence[], blockReason, verifiedBy, verifiedAt`
- Mechanic completes with required evidence → `waiting` (Awaiting verification). Owner verifies → vehicle may move stage.
- Blocked → notify owner with vehicle + reason; task stays open.
- Uploads keep a local draft when offline and retry.

### Approval `apv*`
`id, kind (send message|bid|parts order|quote…), version, status (pending|executing|confirmed|declined), body, expires, sources[], policyVersion, runId, receipt`
- Editing creates a new version and invalidates the old one. Execution must return a receipt (Gmail id, Stripe id…) before status = confirmed.

### Activity `ACTS`
`time, actor (person|AZKT|external), what, entityKind, entityId, kind (automation|approval|message|task|fact), state, receipt, run, policy, sources`. Append-only. Every automated action writes one row with a receipt.

### Finance `FIN`
Feeds (bank, Stripe) are read-only lines: `date, line, counterparty, amount, tab (recon|recv|pay|cost), vehicleId, proposedMatch`. Owner confirms matches (`finMatched`). Vehicle cost = landed + recon lines (`money[]` on vehicle).

### Contacts `CONTACTS`
Derived: buyers = leads; vendors/exporters/carriers are their own table: `id, tab, name, contact, rel, state, linked[], promises[], last, consent, actions[]`.

### Notifications
Bell items = overdue sales tasks + blocked/overdue attention items (high), approvals + tasks due in 24 h (today), other attention (later). Badge count = high + today; red if any high, else amber. Same source feeds mobile sheet and desktop dropdown. Respect `settings.remInApp`.

### Settings `settings`
`remTask, remOver, remDigest, digestTime, remPaid, remInApp, autoSend, partsCap (100|250|500|1000)`, connections `conn{}` with per-connection `state (ok|warn|bad), lastSync`.

## Agents
Six agents: manager, sourcing, shipping, listings, money, customer replies. Each exposes `health`, `doing`, live status lines, and a chat thread (`agentMsgs[agentId]`). Replies stream token-by-token. Agents never change saved data without a person applying the proposal; proposals land in Activity. Manager agent routes requests to the right agent.

## Emails (see `AZKT Email Reminders.dc.html`)
1. Task reminder — at offset. 2. Overdue — 1 h after. 3. Morning digest — `digestTime`, only when non-empty. 4. Deposit paid — instant, owner only. Subject = action + person + time. Buttons deep-link to lead / tasks / Sale tab; text links do done / snooze / reschedule.

## Screen map (desktop `screen`, mobile `oScreen` / `mScreen`)
Owner & manager desktop: home, vehicles, detail, irq (list/detail via `irqId`), tasksAll, inbox, sales (board/tasks via `salesView`, pipe via `salesPipe`), agents, contacts, activity, finance, team (owner) / people (manager), settings, stub. Inspector (`insp`): ask | source | lead.
Owner & manager phone: home, vehicles, veh (detail), sales, agents, more → team/people, inbox (+ `oThread`), irq, activity, settings.
Mechanic desktop/phone: tasks, task, vehicles, more → help, account.

## Non-negotiables from CLAUDE.md
Light default with dark toggle; four health colours always paired with a label; chips never wrap internally; plain-language labels in routine screens, technical terms only inside expanders; every visible control works or is explicitly disabled with a reason.
