"""ORM только технических reservations общего admission ledger."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from memes_search.infrastructure.persistence.models import Base


class AdmissionTable(StrEnum):
    """Стабильное имя принадлежащей primary Search таблицы."""

    RESERVATIONS = "search_sample_admissions"


class SampleAdmissionRow(Base):
    """Слот без файла, приватного текста, признаков или учётных секретов."""

    __tablename__ = AdmissionTable.RESERVATIONS
    __table_args__ = (
        Index("ix_search_sample_admissions_owner", "owner_id", "expires_at"),
        Index("ix_search_sample_admissions_expiry", "expires_at"),
        CheckConstraint("expires_at > created_at", name="ck_search_sample_admission_lifetime"),
    )

    sample_id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
