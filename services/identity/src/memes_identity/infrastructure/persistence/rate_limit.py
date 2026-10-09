"""Распределённые счётчики входа в PostgreSQL."""

from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from hmac import new

from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from memes_identity.application.ports.rate_limit import ILoginRateLimiter
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.models import RateBucketRow
from memes_kernel.values import Number


class RateSubject(StrEnum):
    """Разделённые пространства ключей ограничения."""

    ACCOUNT = "account"
    IP = "ip"


class SqlLoginRateLimiter(ILoginRateLimiter):
    """Атомарные fixed-window счётчики, общие для всех процессов."""

    def __init__(self, session: AsyncSession, settings: IdentitySettings) -> None:
        """Получить транзакцию и параметры лимитов.

        Args:
            session: PostgreSQL-сессия.
            settings: Лимиты и ключ HMAC.
        """
        self._db = session
        self._settings = settings

    async def consume(self, *, account: str, peer_ip: str, now: datetime) -> bool:
        """Атомарно учесть аккаунт и IP без раскрытия низкоэнтропийных значений.

        Args:
            account: Исходный нормализованный логин.
            peer_ip: Сетевой IP без forwarded-заголовков.
            now: Серверное время.
        """
        subjects = (
            (RateSubject.ACCOUNT, account, self._settings.login_account_attempts),
            (RateSubject.IP, peer_ip, self._settings.login_ip_attempts),
        )
        keys = sorted(
            (
                new(
                    self._settings.internal_api_key.encode(),
                    f"{subject}:{value}".encode(),
                    sha256,
                ).hexdigest(),
                limit,
            )
            for subject, value, limit in subjects
        )
        cutoff = now - timedelta(seconds=self._settings.login_window_seconds)
        allowed = True
        for key, limit in keys:
            expired = RateBucketRow.window_started_at <= cutoff
            count = await self._db.scalar(
                insert(RateBucketRow)
                .values(key=key, window_started_at=now, attempts=Number.ONE)
                .on_conflict_do_update(
                    index_elements=[RateBucketRow.key],
                    set_={
                        "window_started_at": case(
                            (expired, now), else_=RateBucketRow.window_started_at
                        ),
                        "attempts": case(
                            (expired, Number.ONE), else_=RateBucketRow.attempts + Number.ONE
                        ),
                    },
                )
                .returning(RateBucketRow.attempts)
            )
            allowed = allowed and count is not None and count <= limit
        return allowed
