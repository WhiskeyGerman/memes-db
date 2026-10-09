"""Проверенная идентичность без HTTP и пользовательских токенов домена."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class PrincipalRole(StrEnum):
    """Права, необходимые прикладным сценариям Processing."""

    USER = "user"
    OPERATOR = "operator"


@dataclass(frozen=True, slots=True, kw_only=True)
class Principal:
    """Только сведения, необходимые проверке разрешения."""

    id: UUID
    roles: frozenset[PrincipalRole]
