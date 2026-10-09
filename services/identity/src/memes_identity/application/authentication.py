"""Совместимые публичные имена прежнего модуля контрактов.

Интерфейсы и DTO определены в отдельных пакетах; модуль только реэкспортирует их.
"""

from memes_identity.application.dto.authentication import (
    IssuedTokens,
    PasswordLimit,
    PasswordPolicy,
)
from memes_identity.application.errors import IdentityFailure as AuthenticationFailure
from memes_identity.application.ports.passwords import IPasswordVerifier
from memes_identity.application.ports.sessions import ISessionService
from memes_identity.application.ports.users import IUserRepository

__all__ = [
    "AuthenticationFailure",
    "IPasswordVerifier",
    "ISessionService",
    "IUserRepository",
    "IssuedTokens",
    "PasswordLimit",
    "PasswordPolicy",
]
