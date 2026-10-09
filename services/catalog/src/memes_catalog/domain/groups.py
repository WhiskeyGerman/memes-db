"""Группы вариаций и эпизоды принадлежности медиа."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import NewType
from uuid import UUID

from memes_catalog.domain.media import MediaId
from memes_kernel.entity import Entity
from memes_kernel.errors import DomainError
from memes_kernel.values import Number, Revision, require_identifier, require_ordered

GroupId = NewType("GroupId", UUID)
MembershipId = NewType("MembershipId", UUID)


class GroupState(StrEnum):
    """Состояние группы."""

    ACTIVE = "active"
    MERGED = "merged"
    EMPTY = "empty"


class MembershipEnd(StrEnum):
    """Причина завершения эпизода членства."""

    DETACHED = "detached"
    MERGED = "merged"
    MEDIA_DELETED = "media_deleted"


class GroupViolation(StrEnum):
    """Нарушения правил состава групп."""

    INVALID_GROUP = "invalid_group"
    INACTIVE_GROUP = "inactive_group"
    SAME_GROUP = "same_group"
    STALE_GROUP = "stale_group"
    INVALID_MEMBERSHIP = "invalid_membership"
    CLOSED_MEMBERSHIP = "closed_membership"
    INVALID_TARGET = "invalid_merge_target"


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupRef:
    """Снимок версии состава группы."""

    id: GroupId
    composition_revision: Revision

    def __post_init__(self) -> None:
        """Проверить идентификатор группы."""
        require_identifier(self.id)


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class VariationGroup(Entity[GroupId]):
    """Группа без загрузки всей коллекции участников."""

    created_at: datetime
    member_count: int = Number.ONE
    composition_revision: Revision = field(default_factory=Revision)
    state: GroupState = GroupState.ACTIVE
    successor_id: GroupId | None = None

    def __post_init__(self) -> None:
        """Проверить согласованность состояния и числа участников."""
        Entity.__post_init__(self)
        require_ordered(self.created_at, self.created_at)
        if self.state is GroupState.ACTIVE:
            valid = self.member_count >= Number.ONE and self.successor_id is None
        elif self.state is GroupState.MERGED:
            valid = self.member_count == Number.ZERO and self.successor_id is not None
        else:
            valid = self.member_count == Number.ZERO and self.successor_id is None
        if not valid:
            raise DomainError(GroupViolation.INVALID_GROUP)
        if self.successor_id is not None:
            require_identifier(self.successor_id)
            if self.successor_id == self.id:
                raise DomainError(GroupViolation.SAME_GROUP)

    @property
    def reference(self) -> GroupRef:
        """Получить снимок текущего состава."""
        return GroupRef(id=self.id, composition_revision=self.composition_revision)

    def require_active(self) -> None:
        """Потребовать действующую группу."""
        if self.state is not GroupState.ACTIVE:
            raise DomainError(GroupViolation.INACTIVE_GROUP)

    def require_reference(self, reference: GroupRef) -> None:
        """Проверить актуальность пользовательского снимка.

        Args:
            reference: Состав, который подтвердил пользователь.

        Raises:
            DomainError: Группа неактивна или состав изменился.
        """
        self.require_active()
        if reference != self.reference:
            raise DomainError(GroupViolation.STALE_GROUP)

    def retire_into(self, successor_id: GroupId) -> "VariationGroup":
        """Завершить группу после объединения.

        Args:
            successor_id: Новая группа-преемник.
        """
        self.require_active()
        return replace(
            self,
            member_count=Number.ZERO,
            state=GroupState.MERGED,
            successor_id=successor_id,
            composition_revision=self.composition_revision.next(),
            revision=self.revision.next(),
        )

    def remove_member(self) -> "VariationGroup":
        """Уменьшить состав после удаления или открепления одного участника."""
        self.require_active()
        remaining = self.member_count - Number.ONE
        return replace(
            self,
            member_count=remaining,
            state=GroupState.ACTIVE if remaining else GroupState.EMPTY,
            composition_revision=self.composition_revision.next(),
            revision=self.revision.next(),
        )


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class GroupMembership(Entity[MembershipId]):
    """Один эпизод нахождения медиа в группе."""

    media_id: MediaId
    group_id: GroupId
    joined_at: datetime
    ended_at: datetime | None = None
    end_reason: MembershipEnd | None = None

    def __post_init__(self) -> None:
        """Проверить идентификаторы и временную согласованность эпизода."""
        Entity.__post_init__(self)
        require_identifier(self.media_id)
        require_identifier(self.group_id)
        require_ordered(self.joined_at, self.joined_at)
        if (self.ended_at is None) != (self.end_reason is None):
            raise DomainError(GroupViolation.INVALID_MEMBERSHIP)
        if self.ended_at is not None:
            require_ordered(self.ended_at, self.joined_at)

    @property
    def active(self) -> bool:
        """Проверить, действует ли эпизод."""
        return self.ended_at is None

    def close(self, *, reason: MembershipEnd, now: datetime) -> "GroupMembership":
        """Завершить эпизод, сохраняя идентичность его голосования.

        Args:
            reason: Причина завершения.
            now: Серверное время.

        Raises:
            DomainError: Эпизод уже закрыт другим действием.
        """
        if not self.active:
            if self.end_reason is reason:
                return self
            raise DomainError(GroupViolation.CLOSED_MEMBERSHIP)
        require_ordered(now, self.joined_at)
        return replace(self, ended_at=now, end_reason=reason, revision=self.revision.next())


@dataclass(frozen=True, slots=True, kw_only=True)
class MergeTarget:
    """Каноническая пара снимков групп для объединения."""

    first: GroupRef
    second: GroupRef

    def __post_init__(self) -> None:
        """Потребовать разные группы и канонический порядок."""
        if self.first.id == self.second.id:
            raise DomainError(GroupViolation.SAME_GROUP)
        if self.first.id.int > self.second.id.int:
            raise DomainError(GroupViolation.INVALID_TARGET)

    @classmethod
    def between(cls, first: GroupRef, second: GroupRef) -> "MergeTarget":
        """Создать цель независимо от порядка выбора.

        Args:
            first: Первый выбранный состав.
            second: Второй выбранный состав.
        """
        left, right = sorted((first, second), key=lambda item: item.id.int)
        return cls(first=left, second=right)


@dataclass(frozen=True, slots=True, kw_only=True)
class DetachTarget:
    """Цель открепления конкретного эпизода членства."""

    membership_id: MembershipId

    def __post_init__(self) -> None:
        """Проверить идентичность эпизода."""
        require_identifier(self.membership_id)
