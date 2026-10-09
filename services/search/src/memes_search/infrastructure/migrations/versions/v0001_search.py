"""Создать поисковые проекции, inbox, outbox и временные приватные данные."""

from enum import StrEnum

from alembic import op
from sqlalchemy import Boolean, Column, DateTime, ForeignKeyConstraint, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "search_0001"
down_revision = None
branch_labels = None
depends_on = None


class MigrationSql(StrEnum):
    """Воспроизводимая схема PostgreSQL-расширений и производных индексов."""

    VECTOR_EXTENSION = "CREATE EXTENSION IF NOT EXISTS vector"
    TRIGRAM_EXTENSION = "CREATE EXTENSION IF NOT EXISTS pg_trgm"
    TEXT_COLUMN = """
        ALTER TABLE search_features ADD COLUMN search_vector tsvector
          GENERATED ALWAYS AS
          (to_tsvector('russian'::regconfig, text) ||
           to_tsvector('english'::regconfig, text)) STORED
    """
    TEXT_INDEX = "CREATE INDEX search_features_fts ON search_features USING gin(search_vector)"
    TRIGRAM_INDEX = (
        "CREATE INDEX search_features_trigram ON search_features USING gin(text gin_trgm_ops)"
    )
    VECTOR_TABLE = """
        CREATE TABLE search_vectors (
          media_id uuid NOT NULL, kind text NOT NULL, ordinal integer NOT NULL,
          space_id text NOT NULL, "values" vector NOT NULL,
          PRIMARY KEY(media_id, kind, ordinal),
          FOREIGN KEY(media_id,kind) REFERENCES search_features(media_id,kind) ON DELETE CASCADE
        )
    """
    VECTOR_INDEX = """
        CREATE INDEX search_vectors_hnsw_2048
        ON search_vectors USING hnsw (("values"::halfvec(2048)) halfvec_cosine_ops)
        WHERE vector_dims("values") = 2048
    """
    VECTOR_SPACE = "CREATE INDEX search_vectors_space ON search_vectors(space_id)"
    DROP_VECTOR = "DROP TABLE search_vectors"


def upgrade() -> None:
    """Применить расширения и собственную схему Search."""
    op.execute(MigrationSql.VECTOR_EXTENSION)
    op.execute(MigrationSql.TRIGRAM_EXTENSION)
    op.create_table(
        "search_media",
        Column("media_id", UUID(as_uuid=True), primary_key=True),
        Column("group_id", UUID(as_uuid=True), nullable=True),
        Column("group_revision", Integer, nullable=True),
        Column("revision", Integer, nullable=False),
        Column("active", Boolean, nullable=False),
        Column("media_kind", String, nullable=True),
        Column("published_at", DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "search_media_active_kind_time", "search_media", ["active", "media_kind", "published_at"]
    )
    op.create_table(
        "search_features",
        Column("media_id", UUID(as_uuid=True), primary_key=True),
        Column("kind", String, primary_key=True),
        Column("generation", Integer, nullable=False),
        Column("cycle_id", UUID(as_uuid=True), nullable=False),
        Column("state", String, nullable=False),
        Column("version_key", Text, nullable=False),
        Column("text", Text, nullable=False),
        ForeignKeyConstraint(["media_id"], ["search_media.media_id"]),
    )
    op.execute(MigrationSql.TEXT_COLUMN)
    op.execute(MigrationSql.TEXT_INDEX)
    op.execute(MigrationSql.TRIGRAM_INDEX)
    op.execute(MigrationSql.VECTOR_TABLE)
    op.execute(MigrationSql.VECTOR_INDEX)
    op.execute(MigrationSql.VECTOR_SPACE)
    op.create_table(
        "search_inbox",
        Column("event_id", UUID(as_uuid=True), primary_key=True),
        Column("received_at", DateTime(timezone=True), nullable=False),
    )
    for table in ("search_sessions", "search_requests"):
        op.create_table(
            table,
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column("body", JSONB, nullable=False),
            Column("expires_at", DateTime(timezone=True), nullable=False),
        )
        op.create_index(f"ix_{table}_expires_at", table, ["expires_at"])
    op.create_table(
        "search_samples",
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("owner_id", UUID(as_uuid=True), nullable=False),
        Column("state", String, nullable=False),
        Column("body", JSONB, nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_search_samples_owner_id", "search_samples", ["owner_id"])
    op.create_index("ix_search_samples_expires_at", "search_samples", ["expires_at"])
    op.create_table(
        "search_sample_features",
        Column("sample_id", UUID(as_uuid=True), primary_key=True),
        Column("kind", String, primary_key=True),
        Column("generation", Integer, nullable=False),
        Column("body", JSONB, nullable=False),
        ForeignKeyConstraint(["sample_id"], ["search_samples.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "search_outbox",
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("sample_id", UUID(as_uuid=True), nullable=True),
        Column("task_id", UUID(as_uuid=True), nullable=True),
        Column("action", String, nullable=False),
        Column("body", JSONB, nullable=False),
        Column("attempts", Integer, nullable=False),
        Column("available_at", DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_search_outbox_available_at", "search_outbox", ["available_at"])


def downgrade() -> None:
    """Удалить собственную схему, сохранив shared PostgreSQL extensions."""
    for table in (
        "search_outbox",
        "search_sample_features",
        "search_samples",
        "search_requests",
        "search_sessions",
        "search_inbox",
    ):
        op.drop_table(table)
    op.execute(MigrationSql.DROP_VECTOR)
    op.drop_table("search_features")
    op.drop_table("search_media")
