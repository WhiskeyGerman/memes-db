"""Сохранить интервалы векторов и текстовые фрагменты для таймкодов."""

from enum import StrEnum

from alembic import op
from sqlalchemy import Column, ForeignKeyConstraint, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

revision = "search_0003"
down_revision = "search_0002"
branch_labels = None
depends_on = None


class MigrationSql(StrEnum):
    """Восстанавливаемый индекс отдельных текстовых фрагментов."""

    GENERATED = """
        ALTER TABLE search_text_fragments ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS
        (to_tsvector('russian'::regconfig, text) ||
         to_tsvector('english'::regconfig, text)) STORED
    """
    FTS = "CREATE INDEX search_fragments_fts ON search_text_fragments USING gin(search_vector)"
    TRIGRAM = (
        "CREATE INDEX search_fragments_trigram ON search_text_fragments "
        "USING gin(text gin_trgm_ops)"
    )
    BACKFILL = """
        INSERT INTO search_text_fragments(media_id,kind,ordinal,text,language)
        SELECT media_id,kind,0,text,'und' FROM search_features WHERE text <> ''
    """


def upgrade() -> None:
    """Добавить nullable интервалы и backfill существующего текста."""
    for column in ("start_ms", "end_ms"):
        op.add_column("search_vectors", Column(column, Integer, nullable=True))
    op.create_check_constraint(
        "ck_search_vector_span",
        "search_vectors",
        "(start_ms IS NULL AND end_ms IS NULL) OR "
        "(start_ms IS NOT NULL AND end_ms IS NOT NULL AND start_ms >= 0 AND end_ms > start_ms)",
    )
    op.create_table(
        "search_text_fragments",
        Column("media_id", UUID(as_uuid=True), primary_key=True),
        Column("kind", String, primary_key=True),
        Column("ordinal", Integer, primary_key=True),
        Column("text", Text, nullable=False),
        Column("language", String, nullable=False),
        Column("start_ms", Integer, nullable=True),
        Column("end_ms", Integer, nullable=True),
        ForeignKeyConstraint(
            ["media_id", "kind"],
            ["search_features.media_id", "search_features.kind"],
            ondelete="CASCADE",
        ),
    )
    op.create_check_constraint(
        "ck_search_text_span",
        "search_text_fragments",
        "(start_ms IS NULL AND end_ms IS NULL) OR "
        "(start_ms IS NOT NULL AND end_ms IS NOT NULL AND start_ms >= 0 AND end_ms > start_ms)",
    )
    op.execute(MigrationSql.GENERATED)
    op.execute(MigrationSql.FTS)
    op.execute(MigrationSql.TRIGRAM)
    op.execute(MigrationSql.BACKFILL)


def downgrade() -> None:
    """Удалить только производные интервалы и текстовые индексы."""
    op.drop_table("search_text_fragments")
    op.drop_constraint("ck_search_vector_span", "search_vectors", type_="check")
    op.drop_column("search_vectors", "end_ms")
    op.drop_column("search_vectors", "start_ms")
