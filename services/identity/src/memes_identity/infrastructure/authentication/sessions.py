"""Opaque access/refresh tokens с транзакционной ротацией PostgreSQL."""

from datetime import datetime, timedelta
from enum import IntEnum
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from memes_identity.application.dto.authentication import (
    AuthenticatedActor,
    IssuedTokens,
    SessionRotation,
)
from memes_identity.application.errors import IdentityFailure
from memes_identity.application.ports.sessions import ISessionService
from memes_identity.domain.user import UserId, UserRole, UserState
from memes_identity.infrastructure.auth_records import TokenKind
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.models import SessionRow, TokenRow, UserRow
from memes_kernel.values import Number


class TokenEntropy(IntEnum):
    """Размер случайного токена в байтах до base64url."""

    BYTES = 32


def token_digest(token: str) -> str:
    """Получить digest высокоэнтропийного токена без сохранения секрета.

    Args:
        token: Непрозрачный секрет.

    Returns:
        str: Hex SHA-256.
    """
    return sha256(token.encode()).hexdigest()


class SqlSessionService(ISessionService):
    """Opaque-токены; replay отзывает всё семейство под блокировкой."""

    def __init__(self, session: AsyncSession, settings: IdentitySettings) -> None:
        """Получить текущую транзакцию и конфигурацию.

        Args:
            session: Сессия PostgreSQL.
            settings: Сроки и лимит активных семейств.
        """
        self._db = session
        self._settings = settings

    async def issue(self, user_id: UserId, now: datetime) -> IssuedTokens:
        """Создать семейство с фиксированным абсолютным сроком.

        Args:
            user_id: Владелец.
            now: Серверное время.
        """
        await self._db.scalar(select(UserRow).where(UserRow.id == user_id).with_for_update())
        active = (
            await self._db.scalars(
                select(SessionRow)
                .where(
                    SessionRow.user_id == user_id,
                    SessionRow.revoked_at.is_(None),
                    SessionRow.expires_at > now,
                )
                .order_by(SessionRow.created_at, SessionRow.id)
                .with_for_update()
            )
        ).all()
        excess = len(active) - self._settings.max_sessions + Number.ONE
        for old in active[: max(excess, Number.ZERO)]:
            await self.revoke(old.id, now)
        family = SessionRow(
            id=uuid4(),
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self._settings.refresh_ttl_seconds),
            revoked_at=None,
        )
        self._db.add(family)
        await self._db.flush()
        tokens, _ = self._mint(family, now)
        await self._db.flush()
        return tokens

    async def authenticate(self, access_token: str, now: datetime) -> AuthenticatedActor | None:
        """Проверить актуальные аккаунт, семейство и access token.

        Args:
            access_token: Секрет клиента.
            now: Серверное время.
        """
        row = (
            await self._db.execute(
                select(UserRow, SessionRow)
                .join(SessionRow, SessionRow.user_id == UserRow.id)
                .join(TokenRow, TokenRow.session_id == SessionRow.id)
                .where(
                    TokenRow.digest == token_digest(access_token),
                    TokenRow.kind == TokenKind.ACCESS,
                    TokenRow.expires_at > now,
                    TokenRow.revoked_at.is_(None),
                    SessionRow.expires_at > now,
                    SessionRow.revoked_at.is_(None),
                    UserRow.state == UserState.ACTIVE,
                )
            )
        ).first()
        if row is None:
            return None
        user, family = row
        if user.login is None:
            return None
        return AuthenticatedActor(
            user_id=UserId(user.id),
            login=user.login,
            roles=frozenset(UserRole(role) for role in user.roles),
            session_id=family.id,
        )

    async def rotate(self, refresh_token: str, now: datetime) -> SessionRotation:
        """Сериализовать ротацию и отозвать семейство при повторе старого токена.

        Args:
            refresh_token: Cookie-секрет.
            now: Серверное время.
        """
        token = await self._db.scalar(
            select(TokenRow).where(
                TokenRow.digest == token_digest(refresh_token), TokenRow.kind == TokenKind.REFRESH
            )
        )
        if token is None:
            return SessionRotation(failure=IdentityFailure.UNAUTHORIZED)
        family = await self._db.get(SessionRow, token.session_id)
        if family is None:
            return SessionRotation(failure=IdentityFailure.UNAUTHORIZED)
        user = await self._db.scalar(
            select(UserRow)
            .where(UserRow.id == family.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        family = await self._db.scalar(
            select(SessionRow)
            .where(SessionRow.id == token.session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        token = await self._db.scalar(
            select(TokenRow)
            .where(TokenRow.digest == token_digest(refresh_token))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            family is None
            or token is None
            or user is None
            or user.state != UserState.ACTIVE
            or family.revoked_at is not None
            or family.expires_at <= now
            or token.expires_at <= now
        ):
            return SessionRotation(failure=IdentityFailure.UNAUTHORIZED)
        if token.consumed_at is not None:
            await self.revoke(family.id, now)
            return SessionRotation(failure=IdentityFailure.REFRESH_REPLAY)
        if token.revoked_at is not None:
            return SessionRotation(failure=IdentityFailure.UNAUTHORIZED)
        token.consumed_at = now
        await self._db.execute(
            update(TokenRow)
            .where(
                TokenRow.session_id == family.id,
                TokenRow.kind == TokenKind.ACCESS,
                TokenRow.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        tokens, replacement = self._mint(family, now)
        token.replacement_id = replacement
        await self._db.flush()
        return SessionRotation(tokens=tokens)

    async def revoke_all(
        self, user_id: UserId, now: datetime, *, except_session_id: UUID | None = None
    ) -> None:
        """Отозвать все семейства, кроме сохраняемой текущей сессии.

        Args:
            user_id: Владелец.
            now: Серверное время.
            except_session_id: Сохраняемая сессия.
        """
        query = select(SessionRow.id).where(SessionRow.user_id == user_id)
        if except_session_id is not None:
            query = query.where(SessionRow.id != except_session_id)
        ids = tuple((await self._db.scalars(query)).all())
        if not ids:
            return
        await self._db.execute(
            update(SessionRow)
            .where(SessionRow.id.in_(ids), SessionRow.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await self._db.execute(
            update(TokenRow)
            .where(TokenRow.session_id.in_(ids), TokenRow.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def revoke(self, session_id: UUID, now: datetime) -> None:
        """Отозвать семейство и все его токены.

        Args:
            session_id: Семейство.
            now: Серверное время.
        """
        await self._db.execute(
            update(SessionRow)
            .where(SessionRow.id == session_id, SessionRow.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await self._db.execute(
            update(TokenRow)
            .where(TokenRow.session_id == session_id, TokenRow.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def delete(self, user_id: UserId) -> None:
        """Удалить семейства; каскад удаляет все их token digest.

        Args:
            user_id: Удаляемый владелец.
        """
        await self._db.execute(delete(SessionRow).where(SessionRow.user_id == user_id))

    def _mint(self, family: SessionRow, now: datetime) -> tuple[IssuedTokens, UUID]:
        """Выпустить секреты и записать только digest.

        Args:
            family: Действующее семейство.
            now: Время выпуска.

        Returns:
            tuple[IssuedTokens, UUID]: Секреты и идентификатор refresh-записи.
        """
        access = token_urlsafe(TokenEntropy.BYTES)
        refresh = token_urlsafe(TokenEntropy.BYTES)
        access_expires = min(
            now + timedelta(seconds=self._settings.access_ttl_seconds), family.expires_at
        )
        refresh_id = uuid4()
        self._db.add_all(
            [
                TokenRow(
                    id=uuid4(),
                    session_id=family.id,
                    kind=TokenKind.ACCESS,
                    digest=token_digest(access),
                    issued_at=now,
                    expires_at=access_expires,
                    consumed_at=None,
                    revoked_at=None,
                    replacement_id=None,
                ),
                TokenRow(
                    id=refresh_id,
                    session_id=family.id,
                    kind=TokenKind.REFRESH,
                    digest=token_digest(refresh),
                    issued_at=now,
                    expires_at=family.expires_at,
                    consumed_at=None,
                    revoked_at=None,
                    replacement_id=None,
                ),
            ]
        )
        return (
            IssuedTokens(
                access_token=access,
                refresh_token=refresh,
                access_expires_at=access_expires,
                refresh_expires_at=family.expires_at,
                session_id=family.id,
            ),
            refresh_id,
        )
