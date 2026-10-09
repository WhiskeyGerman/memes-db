"""Argon2id без блокировки event loop и без сохранения исходного пароля."""

from asyncio import to_thread
from enum import IntEnum
from secrets import token_urlsafe

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from memes_identity.application.ports.passwords import IPasswordVerifier
from memes_identity.infrastructure.configuration import IdentitySettings


class PasswordEntropy(IntEnum):
    """Размер независимого фиктивного секрета."""

    BYTES = 32


class Argon2PasswordVerifier(IPasswordVerifier):
    """Адаптер salted Argon2id с фиктивной проверкой неизвестного аккаунта."""

    def __init__(self, settings: IdentitySettings) -> None:
        """Создать настроенный verifier.

        Args:
            settings: Параметры вычислительной стоимости релиза.
        """
        self._hasher = PasswordHasher(
            time_cost=settings.password_iterations,
            memory_cost=settings.password_memory_kib,
            parallelism=settings.password_parallelism,
            type=Type.ID,
        )
        self._dummy = self._hasher.hash(token_urlsafe(PasswordEntropy.BYTES))

    async def encode(self, password: str) -> str:
        """Вычислить индивидуально salted Argon2id.

        Args:
            password: Исходный пароль без преобразований.
        """
        return await to_thread(self._hasher.hash, password)

    async def verify(self, password: str, verifier: str | None) -> bool:
        """Проверить пароль с одинаковым дорогим путём для неизвестного аккаунта.

        Args:
            password: Предоставленный пароль.
            verifier: Сохранённый хэш либо отсутствие аккаунта.
        """
        try:
            matched = await to_thread(self._hasher.verify, verifier or self._dummy, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        return matched and verifier is not None
