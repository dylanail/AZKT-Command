# Build AZKT from this packet

Version 4.0 · September 14, 2026 · Complete updated handoff; supersedes v3 for implementation.

Build the production AZKT management and agent application described in **AZKT-Full-Build-Spec.md**. Use **Acceptance-Tests.md** as behavioral acceptance criteria and **Decisions-and-Setup.md** for confirmed choices and unresolved connection values. The supplied HTML is a visual reference, not a production backend.

The intended result is a simple web/mobile operations dashboard that organizes inquiries, vehicles, import requests, tasks, evidence, costs, payments and listings; keeps waiting work moving; and lets Dylan direct a capable Manager through the app, Telegram or an authorized external-agent connector. Home includes costs/profit, turnaround and a vehicle milestone timeline.

Required in v4: Dylan can take/upload vehicle photos, speak or type condition notes, and have Manager create a new card or update an existing one with concise condition bullets, linked evidence and actionable tasks. The AI Manager acting for Dylan can read and edit every owner-editable business domain through the same commands as the app, including money and timelines, while retaining the existing evidence/approval process for consequential actions. This is distinct from the human manager employee's permissions.

Expose a generic authenticated MCP connector and equivalent HTTP API to Manager so another agent can ask questions, supply intake evidence, initiate work, continue a mission and retrieve its result. Do not assume a particular external agent name or implementation. Preserve the caller's grant through Manager and return durable results; do not create a second business database or approval bypass. See sections 2.4, 7.4, 10.8 and 10.9 of the full spec and `Changes-in-v4.md`.

Confirmed integrations: **info@azkeitrucks.com**, selected Sebastian/port mail in Dylan's personal Google account, the ledger in his dylxnxil Google Drive, the **Dylan Nail Shipments** folder, **WordPress/WooCommerce**, **Square**, **Telegram**, and **Railway**. Select exact account/source IDs during setup; do not invent missing configuration.

Day one: automate permitted internal work and owner reminders, prepare drafts, and require Dylan's approval for customer sends and publishing. Later, present evidence-backed proposals for specific bounded automatic workflows; never self-enable based on an LLM confidence score. Keep bids, money movement/refunds, material terms/price changes and permissions individually controlled.

Start by inspecting the existing repository and working agents. Preserve useful code and pending work. Report the real data/integration status, map prototype screens to implemented routes, and create the domain schema/commands, durable job/action state, access controls and connection setup. Deliver usable slices: **business inquiry → matched contact/opportunity → reply draft → exact approval → confirmed send**; **timed task → email/Telegram reminder that survives a restart**; and **photos + condition note → correct card + condition bullets + tasks**. Add the external-agent connector over that working Manager service and populate Home metrics from real reconciled data.

Use the latest Liquid Glass II HTML and assets in `reference/design-v2/`. Retain the seven operational responsibilities, physical evidence/owner verification, independent vehicle states, two pre-deposit Sales pipelines, and the earlier sourcing/shipping/Teach workflows. Archived `CLAUDE.md` and README instructions are historical reference material; use the explicit conflict decisions in the full spec.

Build all milestones in sequence and test actual integrations on authorized staging/pilot accounts. Do not stop at a new mockup, fake live activity, hardcoded seed arrays, or an agent that only proposes every internal action. Ask focused questions only when a missing decision blocks a specific feature; continue the unaffected work. Produce a results table showing passed, failed, simulated and setup-blocked acceptance scenarios, deployment/runbook instructions, and the exact features ready for production activation.
