"""calendar sync: additive bookkeeping columns on tasks

Additive only (spec §5.3, §11.2). A task that is an appointment — a call, a meeting, or a follow-up
with a scheduled block — gets a Google Calendar entry, and these columns are what make that sync
idempotent and truthful:

* `calendar_event_id` / `calendar_id` — the entry AZKT wrote and the calendar it lives on. Indexed
  because reconciliation after a lost write result looks a task up by the event id.
* `calendar_synced_revision` / `calendar_synced_hash` — the `schedule_revision` and the exact content
  that were actually pushed. A replayed `task.changed` compares against these and does nothing,
  instead of writing a second entry; a reschedule moves the revision and patches the same entry.
* `calendar_state` / `calendar_error` — synced | cancelled | setup_blocked | failed | unknown | skipped,
  with the provider's own words. `setup_blocked` is how a missing connection or a missing owner
  capability is reported; it is never silently treated as success.
* `calendar_conflicts` — overlapping entries found when the appointment was written. A clash is
  recorded and raised, never a reason to refuse the appointment the owner asked for.

No backfill: every existing task is simply "not synced yet", which is the truth, and the repair sweep
picks up the ones that are still upcoming once the owner connects a calendar and turns writes on.

Revision ID: b8e3f1a24c60
Revises: a7d1c4e90b52
Create Date: 2026-09-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b8e3f1a24c60"
down_revision = "d1b5a8c37e94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("calendar_event_id", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("calendar_id", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("calendar_state", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("calendar_synced_revision", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("calendar_synced_hash", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("calendar_link", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("calendar_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("calendar_error", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("calendar_conflicts", sa.JSON(), nullable=False, server_default="[]"))
    op.create_index(op.f("ix_tasks_calendar_event_id"), "tasks", ["calendar_event_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_tasks_calendar_event_id"), table_name="tasks")
    op.drop_column("tasks", "calendar_conflicts")
    op.drop_column("tasks", "calendar_error")
    op.drop_column("tasks", "calendar_synced_at")
    op.drop_column("tasks", "calendar_link")
    op.drop_column("tasks", "calendar_synced_hash")
    op.drop_column("tasks", "calendar_synced_revision")
    op.drop_column("tasks", "calendar_state")
    op.drop_column("tasks", "calendar_id")
    op.drop_column("tasks", "calendar_event_id")
