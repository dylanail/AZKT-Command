"""inbox slice: additive columns on connections/conversations/messages/drafts

Adds only columns (spec §4.1–4.5 ingestion, admission, classification, drafts and receipts).
Nothing is renamed or removed; every new NOT NULL column carries a server default so the
migration is safe on a populated database.

Revision ID: b1d4f7a20c31
Revises: b41d7e9c2a10
Create Date: 2026-09-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b1d4f7a20c31"
# chained after the reminders/notify columns so the history stays linear (single head)
down_revision = "b41d7e9c2a10"
branch_labels = None
depends_on = None

JSON_OBJ = sa.text("'{}'::json")
JSON_ARR = sa.text("'[]'::json")

CONNECTIONS = [
    sa.Column("coverage_gaps", sa.JSON(), nullable=False, server_default=JSON_ARR),
    sa.Column("excluded_counts", sa.JSON(), nullable=False, server_default=JSON_OBJ),
    sa.Column("capabilities", sa.JSON(), nullable=False, server_default=JSON_OBJ),
    sa.Column("catch_up_state", sa.JSON(), nullable=False, server_default=JSON_OBJ),
]
CONVERSATIONS = [
    sa.Column("account", sa.String(), nullable=True),
    sa.Column("prior_state", sa.String(), nullable=True),
    sa.Column("no_reply_reason", sa.String(), nullable=True),
    sa.Column("match_reasons", sa.JSON(), nullable=False, server_default=JSON_ARR),
    sa.Column("classification_reasons", sa.JSON(), nullable=False, server_default=JSON_ARR),
    sa.Column("classification_source", sa.String(), nullable=False, server_default="deterministic"),
    sa.Column("triage_reason", sa.String(), nullable=True),
    sa.Column("sensitivity", sa.String(), nullable=False, server_default="normal"),
    sa.Column("extra", sa.JSON(), nullable=False, server_default=JSON_OBJ),
]
MESSAGES = [
    sa.Column("provider_thread_id", sa.String(), nullable=True),
    sa.Column("rfc_message_id", sa.String(), nullable=True),
    sa.Column("in_reply_to", sa.String(), nullable=True),
    sa.Column("body_html", sa.Text(), nullable=False, server_default=""),
    sa.Column("history_id", sa.String(), nullable=True),
    sa.Column("label_ids", sa.JSON(), nullable=False, server_default=JSON_ARR),
    sa.Column("admitted", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    sa.Column("admission_rule", sa.String(), nullable=True),
    sa.Column("excluded_reason", sa.String(), nullable=True),
    sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("personal_allowlisted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    sa.Column("attribution", sa.String(), nullable=True),
    sa.Column("suppression", sa.String(), nullable=True),
    sa.Column("extra", sa.JSON(), nullable=False, server_default=JSON_OBJ),
]
DRAFTS = [
    sa.Column("our_message_id", sa.String(), nullable=True),
    sa.Column("external_action_id", sa.String(), nullable=True),
    sa.Column("provider_draft_version", sa.String(), nullable=True),
    sa.Column("provider_draft_synced_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("facts", sa.JSON(), nullable=False, server_default=JSON_OBJ),
    sa.Column("based_on_inbound_id", sa.String(), nullable=True),
    sa.Column("send_decision_version", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("commitments", sa.JSON(), nullable=False, server_default=JSON_ARR),
    sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("receipt", sa.JSON(), nullable=False, server_default=JSON_OBJ),
    sa.Column("generator", sa.String(), nullable=False, server_default="scaffold"),
    sa.Column("extra", sa.JSON(), nullable=False, server_default=JSON_OBJ),
]
TABLES = [("connections", CONNECTIONS), ("conversations", CONVERSATIONS), ("messages", MESSAGES),
          ("drafts", DRAFTS)]
INDEXES = [
    ("ix_conversations_account", "conversations", ["account"]),
    ("ix_messages_provider_thread_id", "messages", ["provider_thread_id"]),
    ("ix_messages_rfc_message_id", "messages", ["rfc_message_id"]),
    ("ix_drafts_our_message_id", "drafts", ["our_message_id"]),
]


def upgrade() -> None:
    for table, columns in TABLES:
        for column in columns:
            op.add_column(table, column)
    for name, table, cols in INDEXES:
        op.create_index(name, table, cols, unique=False)


def downgrade() -> None:
    for name, table, _cols in reversed(INDEXES):
        op.drop_index(name, table_name=table)
    for table, columns in reversed(TABLES):
        for column in reversed(columns):
            op.drop_column(table, column.name)
