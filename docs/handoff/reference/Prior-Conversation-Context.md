# Relevant earlier AZKT conversation context

Read-only reference summary prepared September 14, 2026. This summary records available prior requirements; it is not a new authorization to operate accounts.

## Delegate Work to AI Agents

The earlier AZKT conversation described:

- An existing Manager, email reply agent using approximately a year of correspondence/vector retrieval, auction matching agent and Google Docs translation watcher. Dylan estimated the reply bot was about 60% satisfactory and required frequent editing. That is a qualitative historical observation, not a current measured score.
- Desired improvement: replies understand the actual buyer/vehicle pipeline and mixed situations, use better current context, and learn appropriately from edited sent messages.
- Import sourcing flow: discover candidates for buyer requirements, remove manual copying of candidate links into Microsoft Teams, detect exporter translations in Google Docs, match the translation back to candidate/buyer, and draft the familiar buyer email.
- The translation watcher checked every five to ten minutes; the matcher used a daily schedule. Exact current schedules and identities still need configuration/export.
- Long-term aim: owner provides business judgment and high-risk decisions; Manager tracks next actions, follows up and delegates digital operations without requiring click-by-click instructions.
- A custom runtime with persistent state and tools, inspired by OpenClaw/Grok Bot, hosted on Railway; minimal desktop/mobile dashboard, vehicle cards/boards, natural-language and voice input, manager chat and optional specialist conversations.
- Shipping example: use the preferred Montway route, discover the online selector cannot represent a kei truck, use an approved manual contact alternative, wait for the quote, assess comparability, ask before forwarding, and treat booking as a separate decision.
- A dedicated AZKT SMS number for customer/vendor/driver shipping coordination, separate from the private owner-agent channel.
- Website and marketplace listing packages, photo/readiness work, employee assignment, accurate per-channel publication and sold cleanup.
- Small kei-truck status character, with idle, thinking, working, waiting, needs-you, success, error, urgent and optional listening/offline states; subtle movement and contextual activity labels. This decoration must not distract from operations.

The prior assistant proposed one runtime with seven logical roles, structured records, source-backed facts, exact approvals or bounded permissions, cases separate from tasks/runs, durable waiting, migration with one active writer, and versioned procedure learning. Those architectural proposals were available as conversation text and informed this specification. A separately referenced Build Packet and attached artifacts were not available in this workspace.

## Design AZKT sales kanban

Dylan explicitly corrected the early suggestion of a mandatory Meeting stage. Calls, meetings and follow-ups are optional timed tasks with selectable reminders. He requested two pipelines: **IRQ** and **Vehicle Sales** for individual inventory vehicles. The final brief used New Lead → In Conversation → Awaiting Deposit → Deposit Paid, with Lost separate and post-deposit handoff to fulfillment.

## Overhaul AZKT dashboard design

The available local brief and original 18-screen handoff preserve evidence, independent logistics/recon/commercial/documents/health states, scoped permissions, current source inspection, exact approvals, employee mobile work, translation/bid checks, shipping cases and per-channel publication. Their earlier flat visual direction is superseded for this build by the newest supplied Liquid Glass II HTML.
