"""Размерность pinned Jina nano, lease доставки и fence удалённого владельца."""

from enum import StrEnum

from alembic import op
from sqlalchemy import Column, DateTime
from sqlalchemy.dialects.postgresql import UUID

revision = "search_0002"
down_revision = "search_0001"
branch_labels = None
depends_on = None


class MigrationSql(StrEnum):
    """Явные изменения исторически уже применённой схемы."""

    DROP_2048 = "DROP INDEX search_vectors_hnsw_2048"
    CREATE_768 = """
        CREATE INDEX search_vectors_hnsw_768
        ON search_vectors USING hnsw (("values"::halfvec(768)) halfvec_cosine_ops)
        WHERE vector_dims("values") = 768
    """
    DROP_768 = "DROP INDEX search_vectors_hnsw_768"
    CREATE_2048 = """
        CREATE INDEX search_vectors_hnsw_2048
        ON search_vectors USING hnsw (("values"::halfvec(2048)) halfvec_cosine_ops)
        WHERE vector_dims("values") = 2048
    """


def upgrade() -> None:
    """Добавить fence и lease, перестроить ANN для pinned config 768."""
    op.execute(MigrationSql.DROP_2048)
    op.execute(MigrationSql.CREATE_768)
    op.create_table(
        "search_deleted_owners",
        Column("owner_id", UUID(as_uuid=True), primary_key=True),
        Column("deleted_at", DateTime(timezone=True), nullable=False),
    )
    op.add_column("search_outbox", Column("claim_id", UUID(as_uuid=True), nullable=True))
    op.add_column("search_outbox", Column("claimed_until", DateTime(timezone=True), nullable=True))
    op.create_index("ix_search_outbox_lease", "search_outbox", ["available_at", "claimed_until"])


def downgrade() -> None:
    """Вернуть прежнюю схему только административной операцией."""
    op.drop_index("ix_search_outbox_lease", table_name="search_outbox")
    op.drop_column("search_outbox", "claimed_until")
    op.drop_column("search_outbox", "claim_id")
    op.drop_table("search_deleted_owners")
    op.execute(MigrationSql.DROP_768)
    op.execute(MigrationSql.CREATE_2048)
