"""Продуктовые правила объединения и группирования релевантности."""

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from math import isfinite
from uuid import UUID

from memes_kernel.errors import DomainError
from memes_kernel.values import Number, require_identifier
from memes_search.domain.fragments import MatchedSpan


class RankingLimit(IntEnum):
    """Начальные пределы ранжирования из PRD."""

    FUSION_CONSTANT = 60
    CANDIDATES_PER_BRANCH = 1_000
    GROUPS = 1_000


class RankingViolation(StrEnum):
    """Нарушения целостности кандидатов."""

    INVALID_SCORE = "invalid_relevance_score"
    INVALID_POLICY = "invalid_ranking_policy"
    INCONSISTENT_MEMBERSHIP = "inconsistent_search_membership"


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaCandidate:
    """Релевантность медиа или его фрагмента в одном способе поиска."""

    media_id: UUID
    group_id: UUID
    score: float
    matched_span: MatchedSpan | None = None

    def __post_init__(self) -> None:
        """Проверить идентификаторы и конечность оценки."""
        require_identifier(self.media_id)
        require_identifier(self.group_id)
        if not isfinite(self.score):
            raise DomainError(RankingViolation.INVALID_SCORE)


@dataclass(frozen=True, slots=True, kw_only=True)
class RankedGroup:
    """Группа с наиболее релевантным медиа в качестве представителя."""

    group_id: UUID
    representative_id: UUID
    score: float
    matched_span: MatchedSpan | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RankingPolicy:
    """RRF по медиа с последующим выбором лучшего представителя группы.

    Повторные фрагменты в одной ветви и дополнительные вариации не повышают
    оценку группы. Актуальное членство передаётся из каталога.
    """

    fusion_constant: int = RankingLimit.FUSION_CONSTANT
    candidates_per_branch: int = RankingLimit.CANDIDATES_PER_BRANCH
    maximum_groups: int = RankingLimit.GROUPS

    def __post_init__(self) -> None:
        """Проверить конфигурацию окна и формулы."""
        if min(self.fusion_constant, self.candidates_per_branch, self.maximum_groups) < Number.ONE:
            raise DomainError(RankingViolation.INVALID_POLICY)

    def fuse(
        self,
        branches: tuple[tuple[MediaCandidate, ...], ...],
        *,
        exact_media_id: UUID | None = None,
    ) -> tuple[RankedGroup, ...]:
        """Получить стабильную групповую выдачу по нескольким ветвям.

        Args:
            branches: Оценки фрагментов с актуальными группами.
            exact_media_id: Найденный по SHA-256 оригинал, если он есть среди кандидатов.

        Returns:
            tuple[RankedGroup, ...]: Ограниченное окно упорядоченных групп.
        """
        scores: dict[UUID, float] = {}
        memberships: dict[UUID, UUID] = {}
        witnesses: dict[UUID, tuple[int, MatchedSpan]] = {}
        for branch in branches:
            best: dict[UUID, MediaCandidate] = {}
            for candidate in branch:
                known = memberships.setdefault(candidate.media_id, candidate.group_id)
                if known != candidate.group_id:
                    raise DomainError(RankingViolation.INCONSISTENT_MEMBERSHIP)
                previous = best.get(candidate.media_id)
                if previous is None or (
                    -candidate.score,
                    self._span_key(candidate.matched_span),
                ) < (-previous.score, self._span_key(previous.matched_span)):
                    best[candidate.media_id] = candidate
            ordered = sorted(best.values(), key=lambda item: (-item.score, item.media_id.int))
            for rank, candidate in enumerate(
                ordered[: self.candidates_per_branch], start=Number.ONE
            ):
                scores[candidate.media_id] = scores.get(candidate.media_id, float(Number.ZERO)) + (
                    Number.ONE / (self.fusion_constant + rank)
                )
                if candidate.matched_span is not None:
                    previous_witness = witnesses.get(candidate.media_id)
                    if previous_witness is None or (
                        rank,
                        self._span_key(candidate.matched_span),
                    ) < (previous_witness[0], self._span_key(previous_witness[1])):
                        witnesses[candidate.media_id] = (rank, candidate.matched_span)
        groups: dict[UUID, RankedGroup] = {}
        for media_id, score in scores.items():
            group_id = memberships[media_id]
            previous_group = groups.get(group_id)
            candidate_group = RankedGroup(
                group_id=group_id,
                representative_id=media_id,
                score=score,
                matched_span=witnesses[media_id][1] if media_id in witnesses else None,
            )
            if (
                previous_group is None
                or media_id == exact_media_id
                or (
                    previous_group.representative_id != exact_media_id
                    and (-score, media_id.int)
                    < (-previous_group.score, previous_group.representative_id.int)
                )
            ):
                groups[group_id] = candidate_group
        return tuple(
            sorted(
                groups.values(),
                key=lambda item: (
                    item.representative_id != exact_media_id,
                    -item.score,
                    item.group_id.int,
                ),
            )[: self.maximum_groups]
        )

    @staticmethod
    def _span_key(span: MatchedSpan | None) -> tuple[bool, int, int]:
        """Задать стабильный порядок равнозначных фрагментов."""
        return (
            span is None,
            span.start_ms if span else Number.ZERO,
            span.end_ms if span else Number.ZERO,
        )
