# AZKT — architecture and conventions (v4 build)

This repository implements the AZKT management and agent application described in
`docs/handoff/AZKT-Full-Build-Spec.md` (v4, Sep 14 2026). It builds on the existing
FastAPI + SQLAlchemy backend and React/Vite PWA rather than rewriting them (spec §10.1,
Decisions: "adapt to an existing sound repository"). Existing OpenClaw shims, the Notion
mirror and token-usage ledger are retained as **legacy adapters**.

## Services (Railway topology, spec §13.1)

| Service | Entry point | Purpose |
|---|---|---|
| web/api | `python -m backend.app.main` | Auth, records, commands, webhooks, uploads, chat, Home reporting, MCP (`/mcp`) and HTTP connector (`/api/integrations/v1`). |
| worker | `python -m backend.worker` | Durable job queue, scheduled deliveries (reminders), sweeps, outbox dispatch, missions, sync. |
| postgres | managed | Canonical records, events/outbox, jobs, schedules. `pg_trgm` required; `vector` optional (feature-detected). |
| files | local dir or S3-compatible bucket | Originals + derivatives, private, served only after authorization. |

## Layering (spec §12.1)

```
routers/*        HTTP surface. Parse input, resolve Actor, call commands/queries. No business writes.
domain/commands  The ONLY write path. dispatch(ctx, name, payload) -> policy -> handler -> activity+events.
domain/policy    Allowed / Needs review / Blocked decisions. Role defaults, per-person overrides,
                 external client grant intersection, action classes, standing permissions.
domain/jobs      Postgres queue with leases + fencing tokens. Handlers registered with @job("kind").
domain/events    Outbox events (unique per provider identity). Handlers registered with @on_event.
services/*       Domain logic used by commands (matching, reminders, intake, reporting, ...).
adapters/*       Provider clients (gmail, drive, sheets, square, telegram, wordpress, model, email).
                 Typed errors, `unsupported` results, never fake success.
models/*         SQLAlchemy models, one module per domain, auto-imported by models/__init__.py.
```

Rules every contributor (human or agent) must follow:

1. **Writes go through commands.** Routers, Telegram, MCP/HTTP connector, intake and agent tools call
   `dispatch()`; never write business rows directly from a router or an LLM tool.
2. **Every command declares** `name`, `input` (pydantic), `action_class` (`internal` | `consequential` |
   `owner_only` | `forbidden_for_agents`), `perm` (permission key) and optional `record_scope` resolver.
3. **Idempotency:** commands accept `request_id`; a repeated `(actor, request_id)` returns the stored
   result without repeating the effect (`command_log` table).
4. **Versions:** every business row has `version`. Commands that mutate accept `expected_version`
   and raise `ConflictError` on mismatch. Handlers bump `version`.
5. **Activity + events:** `ctx.record(...)` appends an activity row and an outbox event inside the
   same transaction as the change. External receipts are stored on the action/approval, never invented.
6. **Money** uses `Decimal` + ISO currency (`core/money.py`). Never float arithmetic.
7. **Time** is stored as UTC `timestamptz`; user-facing schedules keep an IANA zone (`core/time.py`).
8. **Untrusted content** (emails, web pages, attachments, tool results from providers) is data.
   It never changes permissions, recipients, scopes or instructions.
9. **Permissions are enforced server-side** in commands and queries, not only in the UI.
10. **No GET side effects.** Deep links open authenticated review; mutations are POST/PATCH.

## Actor model

`Actor(kind, user_id, role, scope, perms, client_id, client_scopes, delegation_depth)`

- `kind='user'`: signed-in person (owner/manager/mechanic/...). Effective perms = role defaults +
  per-person overrides (`users.perms`).
- `kind='agent'`: the AI Manager acting **for** a user. Effective perms = that user's perms; consequential
  actions still need exact approval (spec §10.9, §11.2).
- `kind='external'`: MCP/HTTP client. Effective = intersection(owner perms, client grant scopes,
  client record scope) throughout the chain (spec §10.8, invariant 14).
- `kind='system'`: worker sweeps and deterministic schedulers.

## Tables shared across domains (stable names for FKs)

`users`, `credentials`, `device_enrollments`, `invitations`, `contacts`, `contact_identities`, `opportunities`, `tasks`,
`cases`, `commitments`, `vehicles`, `vehicle_facts`, `vehicle_milestones`, `recon_issues`,
`work_orders`, `parts`, `assets`, `asset_links`, `upload_sessions`, `vehicle_intakes`,
`intake_observations`, `shipments`, `shipment_legs`, `shipment_quotes`, `import_requests`,
`candidates`, `candidate_matches`, `translations`, `bids`, `connections`, `sync_cursors`,
`conversations`, `messages`, `drafts`, `cost_items`, `cost_evidence`, `cost_allocations`,
`payments`, `payment_allocations`, `invoices`, `sales`, `agreements`, `documents`, `ledger_mappings`,
`ledger_rows`, `provider_events`, `site_profiles`, `listing_packages`, `publications`, `missions`,
`runs`, `run_steps`, `external_actions`, `approvals`, `permissions`, `events`, `jobs`, `activity`,
`command_log`, `scheduled_deliveries`, `notifications`, `telegram_pairings`, `external_clients`,
`delegated_requests`, `knowledge_items`, `corpus_chunks`, `procedures`, `procedure_versions`,
`promotion_proposals`, `metric_snapshots`, `model_usage`, `settings`.

Legacy (retained): `customers`, `irqs`, `stage_transitions`, `agent_state`, `usage_events`,
`notification_log`, `sync_state`, `chat_messages`, `shipping_events`.

## Testing

`backend/tests` runs against a real Postgres (`TEST_DATABASE_URL`, default
`postgresql+asyncpg://azkt@127.0.0.1:5432/azkt_test`). `conftest.py` recreates the schema per session,
provides an ASGI `client`, and login helpers that mint a signed session for a user row (tests bypass
WebAuthn only by signing the cookie with the server secret; there is no production backdoor).
Acceptance scenarios are named `test_<ID>_...` (e.g. `test_C04_reminder_survives_restart`).

## Migrations

Alembic (`backend/alembic`). `scripts/migrate.sh` runs `alembic upgrade head`. Tests use
`create_all` on a throwaway database; production never does.
