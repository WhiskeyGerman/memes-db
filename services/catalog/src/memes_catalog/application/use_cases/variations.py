"""Исполнение групповых решений в одной транзакции каталога."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from memes_catalog.application.dto.catalog import Actor, CatalogDefault, VoteOutcome
from memes_catalog.application.ports.catalog import ICatalogUnitOfWorkFactory
from memes_catalog.application.ports.variations import IVariationRepository
from memes_catalog.domain.governance import VariationGovernance
from memes_catalog.domain.groups import DetachTarget, GroupId, GroupRef, MembershipId, MergeTarget
from memes_catalog.domain.media import AuthorId
from memes_catalog.domain.voting import (
    BallotId,
    BallotState,
    DecisionId,
    DismissalId,
    SuggestionDismissal,
    VariationBallot,
    VariationTarget,
    VoteId,
    VotingRule,
)
from memes_kernel.values import Revision
from memes_platform.application.errors import ApplicationError, ErrorCode


class TargetPrefix(StrEnum):
    """Пространства канонических ключей голосования."""

    MERGE = "merge"
    DETACH = "detach"


class VariationUseCases:
    """Оркестрация правил домена без ORM и сетевого транспорта."""

    def __init__(
        self,
        factory: ICatalogUnitOfWorkFactory,
        *,
        merge_threshold: int,
        detach_threshold: int,
        policy_revision: UUID,
        vote_rate: int = CatalogDefault.VOTES_PER_MINUTE,
    ) -> None:
        """Сохранить порты и конфигурацию политики.

        Args:
            factory: Фабрика транзакций.
            merge_threshold: Порог объединения.
            detach_threshold: Порог открепления.
            policy_revision: Версия политики.
            vote_rate: Допустимые действия за минуту.
        """
        self.factory = factory
        self.merge_threshold = merge_threshold
        self.detach_threshold = detach_threshold
        self.policy_revision = policy_revision
        self.vote_rate = vote_rate

    async def _cast(
        self,
        repository: IVariationRepository,
        *,
        target: VariationTarget,
        key: str,
        threshold: int,
        actor: Actor,
        now: datetime,
    ) -> VariationBallot:
        """Согласовать единственный голос и счётчик."""
        ballot = await repository.open_ballot(key)
        if ballot is None:
            ballot = VariationBallot(
                id=BallotId(uuid4()),
                target=target,
                rule=VotingRule(threshold=threshold, policy_revision=self.policy_revision),
                opened_at=now,
            )
            await repository.add_ballot(ballot, key=key)
        await repository.limit_vote_rate(actor.user_id, maximum=self.vote_rate, now=now)
        existing = await repository.vote(ballot.id, actor.user_id)
        new_ballot, vote = ballot.cast(
            vote_id=VoteId(uuid4()),
            voter_id=AuthorId(actor.user_id),
            existing=existing,
            now=now,
        )
        await repository.save_vote(vote)
        await repository.save_ballot(new_ballot)
        return new_ballot

    async def merge(
        self,
        *,
        actor: Actor,
        first_id: UUID,
        second_id: UUID,
        first_revision: int,
        second_revision: int,
        now: datetime,
    ) -> VoteOutcome:
        """Подать голос и объединить весь состав после порога.

        Args:
            actor: Проверенный участник.
            first_id: Первая группа.
            second_id: Вторая группа.
            first_revision: Снимок первого состава.
            second_revision: Снимок второго состава.
            now: Серверное время.
        """
        target = MergeTarget.between(
            GroupRef(id=GroupId(first_id), composition_revision=Revision(value=first_revision)),
            GroupRef(id=GroupId(second_id), composition_revision=Revision(value=second_revision)),
        )
        key = (
            f"{TargetPrefix.MERGE}:{target.first.id}:{target.first.composition_revision.value}:"
            f"{target.second.id}:{target.second.composition_revision.value}"
        )
        async with self.factory.open() as uow:
            groups = await uow.variations.lock_groups((target.first.id, target.second.id))
            if len(groups) != 2:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            first, second = groups
            loaded = {first.id: first, second.id: second}
            loaded[target.first.id].require_reference(target.first)
            loaded[target.second.id].require_reference(target.second)
            ballot = await self._cast(
                uow.variations,
                target=target,
                key=key,
                threshold=self.merge_threshold,
                actor=actor,
                now=now,
            )
            result_id: UUID | None = None
            if ballot.state is BallotState.REACHED:
                change = VariationGovernance.merge(
                    first=first,
                    second=second,
                    ballot=ballot,
                    successor_id=GroupId(uuid4()),
                    decision_id=DecisionId(uuid4()),
                    now=now,
                )
                for group in (change.first, change.second, change.successor):
                    await uow.variations.save_group(group)
                await uow.variations.move_memberships(
                    source_ids=(first.id, second.id), target_id=change.successor.id, now=now
                )
                await uow.variations.save_ballot(change.ballot)
                await uow.variations.save_decision(change.decision)
                await uow.variations.obsolete(
                    group_ids=(first.id, second.id), membership_ids=(), now=now
                )
                await uow.variations.changed((first.id, second.id, change.successor.id), now=now)
                result_id = change.successor.id
            await uow.commit()
            return VoteOutcome(
                ballot_id=ballot.id,
                active_votes=ballot.active_votes,
                threshold=ballot.rule.threshold,
                applied=result_id is not None,
                group_id=result_id,
            )

    async def detach(self, *, actor: Actor, membership_id: UUID, now: datetime) -> VoteOutcome:
        """Открепить конкретный эпизод членства после голосования.

        Args:
            actor: Проверенный участник.
            membership_id: Выбранный эпизод.
            now: Серверное время.
        """
        async with self.factory.open() as uow:
            # Блокировка состава предшествует блокировке голоса.
            membership = await uow.variations.membership(MembershipId(membership_id))
            if membership is None or not membership.active:
                raise ApplicationError(ErrorCode.STALE_STATE)
            groups = await uow.variations.lock_groups((membership.group_id,))
            if not groups:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            membership = await uow.variations.membership(MembershipId(membership_id), lock=True)
            if membership is None or not membership.active:
                raise ApplicationError(ErrorCode.STALE_STATE)
            group = groups[0]
            if group.member_count <= 1:
                raise ApplicationError(ErrorCode.CONFLICT)
            target = DetachTarget(membership_id=membership.id)
            key = f"{TargetPrefix.DETACH}:{membership.id}"
            ballot = await self._cast(
                uow.variations,
                target=target,
                key=key,
                threshold=self.detach_threshold,
                actor=actor,
                now=now,
            )
            result_id: UUID | None = None
            if ballot.state is BallotState.REACHED:
                change = VariationGovernance.detach(
                    group=group,
                    membership=membership,
                    ballot=ballot,
                    singleton_id=GroupId(uuid4()),
                    membership_id=MembershipId(uuid4()),
                    decision_id=DecisionId(uuid4()),
                    now=now,
                )
                for updated_group in (change.remaining, change.singleton):
                    await uow.variations.save_group(updated_group)
                await uow.variations.save_membership(change.closed_membership)
                await uow.variations.save_membership(change.membership)
                await uow.variations.save_ballot(change.ballot)
                await uow.variations.save_decision(change.decision)
                await uow.variations.obsolete(
                    group_ids=(group.id,), membership_ids=(membership.id,), now=now
                )
                await uow.variations.changed((group.id, change.singleton.id), now=now)
                result_id = change.singleton.id
            await uow.commit()
            return VoteOutcome(
                ballot_id=ballot.id,
                active_votes=ballot.active_votes,
                threshold=ballot.rule.threshold,
                applied=result_id is not None,
                group_id=result_id,
            )

    async def withdraw(self, *, actor: Actor, ballot_id: UUID, now: datetime) -> VoteOutcome:
        """Отозвать ещё не исполненный голос.

        Args:
            actor: Участник.
            ballot_id: Голосование.
            now: Серверное время.
        """
        async with self.factory.open() as uow:
            ballot = await uow.variations.ballot(BallotId(ballot_id))
            if ballot is None:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            vote = await uow.variations.vote(ballot.id, actor.user_id)
            if vote is None:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            changed, withdrawn = ballot.withdraw(vote=vote, now=now)
            await uow.variations.save_ballot(changed)
            await uow.variations.save_vote(withdrawn)
            await uow.commit()
            return VoteOutcome(
                ballot_id=changed.id,
                active_votes=changed.active_votes,
                threshold=changed.rule.threshold,
                applied=False,
            )

    async def dismiss(
        self,
        *,
        actor: Actor,
        media_id: UUID,
        group_id: UUID,
        group_revision: int,
        now: datetime,
    ) -> None:
        """Зафиксировать личное решение, не меняющее группы.

        Args:
            actor: Участник.
            media_id: Исходное медиа.
            group_id: Кандидат.
            group_revision: Состав кандидата.
            now: Серверное время.
        """
        async with self.factory.open() as uow:
            view = await uow.get_media(media_id)
            groups = await uow.variations.lock_groups((GroupId(group_id),))
            if view is None or not groups:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            reference = GroupRef(
                id=GroupId(group_id), composition_revision=Revision(value=group_revision)
            )
            groups[0].require_reference(reference)
            dismissal = SuggestionDismissal(
                id=DismissalId(uuid4()),
                user_id=AuthorId(actor.user_id),
                source_media_id=view.media.id,
                candidate_group=reference,
                dismissed_at=now,
            )
            await uow.variations.dismiss(dismissal)
            await uow.commit()
