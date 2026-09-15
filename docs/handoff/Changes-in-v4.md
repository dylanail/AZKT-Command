# AZKT v4 changes

September 14, 2026 · This packet includes the complete revised specification and all previous reference designs. Use v4 instead of v3 for implementation.

## Vehicle intake with photos and condition notes

Take pictures in Manager, select photos from the gallery, and describe the vehicle by voice or text. Manager matches the existing card or creates a new one, saves concise condition bullets and source photos/notes, and creates actionable linked tasks. Include interrupted uploads, new vehicles with incomplete identity, corrections, duplicate-task prevention and honest save status. Available through the mobile/desktop app and the paired Telegram intake flow.

Full spec: **section 7.4**; acceptance: **I01–I07**.

## Full business access and editing through Manager

The AI Manager acting for Dylan can read and update all owner-editable business records, including vehicle details, photos, tasks, customers, import requests, shipping, money and timelines. Routine authorized internal edits save directly. Existing evidence and consequential-action approvals remain in place. The human manager employee role is separate; limited employees/connector clients do not gain owner access by talking to Manager.

Full spec: **section 10.9**, human access clarification in **section 11.1**; acceptance: **I06, J03**.

## Generic external-agent connector

Provide authenticated MCP and equivalent HTTP access to AZKT Manager. Another agent can ask questions, submit work and photos/notes, continue a mission and fetch its results. Keep the interface independent of any specific agent name or architecture. Use durable request IDs, explicit client scopes, revocation, evidence/results and the existing approval queue.

Full spec: **section 10.8**; acceptance: **J01–J06**.

## Home metrics and timeline

Home now includes vehicles sold, sold-vehicle costs, gross profit and days to sale; additional inventory cost and projected profit where supported; and a vehicle milestone timeline with stage age and next action. Include period filters, source drill-down, currency/FX basis, missing-data labels, estimated versus recorded profit, and role-safe aggregates. Today's urgent work remains visible when viewing a historical reporting period.

Full spec: **section 2.4**; acceptance: **K01–K05**.

## Builder impact

Updated product outcomes, screen/data contracts, commands/events, access rules, Railway topology, milestones, acceptance criteria, coverage map, setup decisions and builder introduction. M1 includes intake, owner Manager coverage and the connector; M2 populates Home's financial metrics from reconciled sources. The original v3 files/ZIP and supplied design references are preserved.
