"""v2 soft delete

Adds is_deleted / deleted_at columns to recordings & tasks; drops the
global file_hash UNIQUE constraint and replaces it with a MySQL 8
functional partial-unique index so logically-deleted rows do not block
new uploads of the same MD5.

Revision ID: 20260912_v2_soft_delete
Revises: c1100b151fca
Create Date: 2026-09-12 19:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "20260912_v2_soft_delete"
down_revision = "c1100b151fca"
branch_labels = None
depends_on = None

_PARTIAL_UNIQUE_NAME = "uk_recordings_file_hash_not_deleted"


def upgrade() -> None:
    # --- recordings -------------------------------------------------
    op.add_column(
        "recordings",
        sa.Column(
            "is_deleted",
            sa.Boolean().with_variant(mysql.TINYINT(unsigned=False), "mysql"),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    op.add_column(
        "recordings",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_recordings_alive_created",
        "recordings",
        ["is_deleted", "created_at"],
    )
    # --- tasks ------------------------------------------------------
    op.add_column(
        "tasks",
        sa.Column(
            "is_deleted",
            sa.Boolean().with_variant(mysql.TINYINT(unsigned=False), "mysql"),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_tasks_alive_status",
        "tasks",
        ["is_deleted", "status"],
    )
    # --- file_hash: global UNIQUE -> partial unique (alive only) ---
    # MySQL 8 functional-index syntax: (expr).  Using CASE WHEN so
    # logically-deleted rows contribute NULL (NULLs don't participate
    # in UNIQUE checks -> multiple deleted rows with the same hash ok).
    op.drop_constraint("uk_recordings_file_hash", "recordings", type_="unique")
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_PARTIAL_UNIQUE_NAME} ON recordings (
          file_hash,
          ((CASE WHEN is_deleted THEN NULL ELSE 0 END))
        );
        """
    )


def downgrade() -> None:
    # NOTE: before downgrade make sure no two live rows share a
    # file_hash, otherwise the GLOBAL UNIQUE recreation below will
    # fail.  Our software never allows this under normal operations.
    # MySQL < 8 does not support DROP INDEX IF EXISTS, so manually
    # check INFORMATION_SCHEMA first.
    conn = op.get_bind()
    exists = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'recordings' "
            "  AND index_name   = :n"
        ),
        {"n": _PARTIAL_UNIQUE_NAME},
    ).scalar()
    if bool(int(exists or 0)):
        op.execute(f"ALTER TABLE recordings DROP INDEX {_PARTIAL_UNIQUE_NAME}")
    op.create_unique_constraint(
        "uk_recordings_file_hash", "recordings", ["file_hash"]
    )
    # Roll back in reverse order of upgrade to avoid 1553 FK/index deps.
    op.drop_index("idx_tasks_alive_status", table_name="tasks")
    op.drop_column("tasks", "deleted_at")
    op.drop_column("tasks", "is_deleted")
    op.drop_index("idx_recordings_alive_created", table_name="recordings")
    op.drop_column("recordings", "deleted_at")
    op.drop_column("recordings", "is_deleted")
