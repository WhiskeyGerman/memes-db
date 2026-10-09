"""Идентичность доменных сущностей."""

from dataclasses import dataclass, field
from typing import Generic, TypeVar
from uuid import UUID

from memes_kernel.values import Revision, require_identifier

IdT = TypeVar("IdT", bound=UUID)


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Entity(Generic[IdT]):
    """Сущность с идентичностью и оптимистической ревизией."""

    id: IdT
    revision: Revision = field(default_factory=Revision)

    def __post_init__(self) -> None:
        """Проверить идентификатор сущности."""
        require_identifier(self.id)

    def __eq__(self, other: object) -> bool:
        """Сравнить сущности по типу и идентичности."""
        return isinstance(other, Entity) and type(self) is type(other) and self.id == other.id

    def __hash__(self) -> int:
        """Вычислить хэш идентичности."""
        return hash((type(self), self.id))
