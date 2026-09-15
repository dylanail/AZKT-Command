"""outbox retry backoff + one connection row per provider

Additive only (spec §12.1):

* `events.next_attempt_at` — a failed outbox handler no longer re-runs on every worker pass; the
  event is retried after a bounded backoff (domain/events.dispatch_pending).
* `uq_connection_provider` — the data model has always intended exactly one `connections` row per
  provider (every writer goes through `services/connections.get(create=True)`), but nothing enforced
  it, so a race could leave two rows and reads picked one arbitrarily. Pre-existing duplicates are
  collapsed onto the deterministic winner (live row, then newest) and their dependants repointed
  before the index is created.

Revision ID: f2a5c81e34b9
Revises: e4b7c91d02fa
Create Date: 2026-09-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "f2a5c81e34b9"
down_revision = "e4b7c91d02fa"
branch_labels = None
depends_on = None

# Same pick as services/connections._PICK_ORDER.
_RANK = ("CASE status WHEN 'connected' THEN 0 WHEN 'warn' THEN 1 WHEN 'degraded' THEN 1 "
         "WHEN 'error' THEN 2 WHEN 'expired' THEN 2 ELSE 3 END, created_at DESC, id DESC")

# (table, column) pairs that carry a connections.id; all are repointed at the surviving row.
_DEPENDANTS = (("sync_cursors", "connection_id"), ("conversations", "connection_id"),
               ("messages", "connection_id"), ("drive_files", "connection_id"),
               ("payments", "connection_id"), ("ledger_mappings", "connection_id"),
               ("site_profiles", "connection_id"), ("drafts", "account_connection_id"))


def upgrade() -> None:
    op.add_column("events", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_events_next_attempt_at"), "events", ["next_attempt_at"], unique=False)

    op.execute(f"""
        CREATE TEMP TABLE _conn_dupes ON COMMIT DROP AS
        SELECT id AS loser, first_value(id) OVER (PARTITION BY provider ORDER BY {_RANK}) AS keeper
        FROM connections
    """)
    op.execute("DELETE FROM _conn_dupes WHERE loser = keeper")
    # A cursor name is derivable state: drop the loser's copy when the keeper already holds that name.
    op.execute("""
        DELETE FROM sync_cursors c USING _conn_dupes d
        WHERE c.connection_id = d.loser
          AND EXISTS (SELECT 1 FROM sync_cursors k WHERE k.connection_id = d.keeper AND k.name = c.name)
    """)
    for table, column in _DEPENDANTS:
        op.execute(f"""
            DO $$ BEGIN
              IF to_regclass('public.{table}') IS NOT NULL THEN
                UPDATE {table} t SET {column} = d.keeper FROM _conn_dupes d WHERE t.{column} = d.loser;
              END IF;
            END $$;
        """)
    op.execute("DELETE FROM connections c USING _conn_dupes d WHERE c.id = d.loser")
    op.create_unique_constraint("uq_connection_provider", "connections", ["provider"])


def downgrade() -> None:
    op.drop_constraint("uq_connection_provider", "connections", type_="unique")
    op.drop_index(op.f("ix_events_next_attempt_at"), table_name="events")
    op.drop_column("events", "next_attempt_at")
