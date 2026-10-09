"""PostgreSQL-адаптер порта вариаций без выбора продуктовых решений."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import String, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from memes_catalog.application.dto.catalog import CatalogDefault
from memes_catalog.application.ports.variations import IVariationRepository
from memes_catalog.domain.groups import (
    GroupId,
    GroupMembership,
    MembershipEnd,
    MembershipId,
    VariationGroup,
)
from memes_catalog.domain.voting import (
    BallotId,
    BallotState,
    SuggestionDismissal,
    VariationBallot,
    VariationDecision,
    VariationVote,
)
from memes_catalog.infrastructure.persistence import mapping
from memes_catalog.infrastructure.persistence.events import DeliveryTarget, delivery_targets
from memes_catalog.infrastructure.persistence.models import (
    BallotRow,
    DecisionRow,
    DismissalRow,
    GroupRow,
    MembershipRow,
    VoteRateRow,
    VoteRow,
)
from memes_kernel.values import Number
from memes_platform.application.errors import ApplicationError, ErrorCode


class PostgresVariations(IVariationRepository):
    """Хранение уже согласованных доменных состояний."""

    def __init__(self, session: AsyncSession) -> None:
        """Сохранить общую транзакцию.

        Args:
            session: Сессия unit of work.
        """
        self.session = session

    async def lock_groups(self, group_ids: tuple[GroupId, ...]) -> tuple[VariationGroup, ...]:
        """Загрузить составы в детерминированном порядке блокировки."""
        rows = await self.session.scalars(
            select(GroupRow)
            .where(GroupRow.id.in_(group_ids))
            .order_by(GroupRow.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return tuple(mapping.group(row) for row in rows)

    async def membership(
        self, membership_id: MembershipId, *, lock: bool = False
    ) -> GroupMembership | None:
        """Получить выбранный эпизод."""
        query = (
            select(MembershipRow)
            .where(MembershipRow.id == membership_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            query = query.with_for_update()
        row = await self.session.scalar(query)
        return None if row is None else mapping.membership(row)

    async def ballot(self, ballot_id: BallotId) -> VariationBallot | None:
        """Получить голосование с блокировкой счётчика."""
        row = await self.session.scalar(
            select(BallotRow).where(BallotRow.id == ballot_id).with_for_update()
        )
        return None if row is None else mapping.ballot(row)

    async def open_ballot(self, key: str) -> VariationBallot | None:
        """Получить единственный действующий ballot цели."""
        row = await self.session.scalar(
            select(BallotRow)
            .where(
                BallotRow.target_key == key,
                BallotRow.state.in_((BallotState.OPEN, BallotState.REACHED)),
            )
            .with_for_update()
        )
        return None if row is None else mapping.ballot(row)

    async def add_ballot(self, ballot: VariationBallot, *, key: str) -> None:
        """Сохранить созданное Application голосование."""
        self.session.add(
            BallotRow(
                id=ballot.id,
                target_key=key,
                target_payload=mapping.target_payload(ballot.target),
                threshold=ballot.rule.threshold,
                policy_revision=ballot.rule.policy_revision,
                opened_at=ballot.opened_at,
                active_votes=ballot.active_votes,
                state=ballot.state.value,
                closed_at=ballot.closed_at,
                decision_id=ballot.decision_id,
                revision=ballot.revision.value,
            )
        )
        await self.session.flush()

    async def vote(self, ballot_id: BallotId, voter_id: UUID) -> VariationVote | None:
        """Получить голос под общей блокировкой ballot."""
        row = await self.session.scalar(
            select(VoteRow).where(VoteRow.ballot_id == ballot_id, VoteRow.voter_id == voter_id)
        )
        return None if row is None else mapping.vote(row)

    async def save_ballot(self, ballot: VariationBallot) -> None:
        """Записать новое состояние счётчика."""
        await self.session.execute(
            update(BallotRow)
            .where(BallotRow.id == ballot.id)
            .values(
                active_votes=ballot.active_votes,
                state=ballot.state.value,
                closed_at=ballot.closed_at,
                decision_id=ballot.decision_id,
                revision=ballot.revision.value,
            )
        )

    async def save_vote(self, vote: VariationVote) -> None:
        """Записать единственный голос участника."""
        await self.session.merge(
            VoteRow(
                id=vote.id,
                ballot_id=vote.ballot_id,
                voter_id=vote.voter_id,
                cast_at=vote.cast_at,
                withdrawn_at=vote.withdrawn_at,
                revision=vote.revision.value,
            )
        )
        await self.session.flush()

    async def save_group(self, group: VariationGroup) -> None:
        """Сохранить вычисленный доменом состав."""
        await self.session.merge(
            GroupRow(
                id=group.id,
                revision=group.revision.value,
                composition_revision=group.composition_revision.value,
                member_count=group.member_count,
                created_at=group.created_at,
                state=group.state.value,
                successor_id=group.successor_id,
            )
        )
        await self.session.flush()

    async def save_membership(self, membership: GroupMembership) -> None:
        """Сохранить согласованный эпизод."""
        await self.session.merge(
            MembershipRow(
                id=membership.id,
                media_id=membership.media_id,
                group_id=membership.group_id,
                joined_at=membership.joined_at,
                ended_at=membership.ended_at,
                end_reason=None if membership.end_reason is None else membership.end_reason.value,
                revision=membership.revision.value,
            )
        )
        await self.session.flush()

    async def move_memberships(
        self, *, source_ids: tuple[GroupId, ...], target_id: GroupId, now: datetime
    ) -> None:
        """Перенести состав SQL-пакетом без материализации больших групп."""
        # Идентичность нового эпизода создаётся PostgreSQL; выбор групп уже сделан Application.
        await self.session.execute(
            text(
                "WITH closed AS (UPDATE catalog_memberships SET ended_at=:now, "
                "end_reason=:reason, "
                "revision=revision+1 WHERE group_id=ANY(:sources) AND ended_at IS NULL "
                "RETURNING media_id) "
                "INSERT INTO catalog_memberships(id, media_id, group_id, joined_at, "
                "ended_at, end_reason, revision) "
                "SELECT gen_random_uuid(), media_id, :target, :now, NULL, NULL, 1 FROM closed"
            ),
            {
                "now": now,
                "reason": MembershipEnd.MERGED.value,
                "sources": list(source_ids),
                "target": target_id,
            },
        )
        # Закрытые эпизоды не должны оставлять активные голосования за открепление.
        ended = select(MembershipRow.id).where(
            MembershipRow.group_id.in_(source_ids), MembershipRow.ended_at == now
        )
        await self.session.execute(
            update(BallotRow)
            .where(
                BallotRow.state.in_((BallotState.OPEN, BallotState.REACHED)),
                BallotRow.target_payload["membership_id"].astext.in_(
                    select(ended.subquery().c.id.cast(String))
                ),
            )
            .values(
                state=BallotState.OBSOLETE.value,
                closed_at=now,
                revision=BallotRow.revision + Number.ONE,
            )
        )

    async def obsolete(
        self,
        *,
        group_ids: tuple[GroupId, ...],
        membership_ids: tuple[MembershipId, ...],
        now: datetime,
    ) -> None:
        """Закрыть потерявшие цель незавершённые голосования."""
        conditions: list[ColumnElement[bool]] = []
        if group_ids:
            values = [str(value) for value in group_ids]
            conditions.extend(
                (
                    BallotRow.target_payload["first_id"].astext.in_(values),
                    BallotRow.target_payload["second_id"].astext.in_(values),
                )
            )
        if membership_ids:
            conditions.append(
                BallotRow.target_payload["membership_id"].astext.in_(
                    [str(value) for value in membership_ids]
                )
            )
        if conditions:
            await self.session.execute(
                update(BallotRow)
                .where(
                    BallotRow.state.in_((BallotState.OPEN, BallotState.REACHED)), or_(*conditions)
                )
                .values(
                    state=BallotState.OBSOLETE.value,
                    closed_at=now,
                    revision=BallotRow.revision + Number.ONE,
                )
            )

    async def save_decision(self, decision: VariationDecision) -> None:
        """Сохранить исторический факт исполнения."""
        payload = mapping.target_payload(decision.target)
        payload.update(
            {
                "id": str(decision.id),
                "ballot_id": str(decision.ballot_id),
                "threshold": decision.rule.threshold,
                "counted_votes": decision.counted_votes,
                "policy_revision": str(decision.rule.policy_revision),
                "group_id": str(decision.resulting_group_id),
                "applied_at": decision.applied_at.isoformat(),
            }
        )
        self.session.add(
            DecisionRow(
                id=decision.id,
                ballot_id=decision.ballot_id,
                payload=payload,
                applied_at=decision.applied_at,
            )
        )

    async def changed(self, group_ids: tuple[GroupId, ...], *, now: datetime) -> None:
        """Увеличить версии проекций и подготовить доставку Search."""
        # PostgreSQL создаёт проекции и outbox в одном пакете. Размер группы
        # не влияет на число ORM-объектов или SQL-запросов процесса API.
        await self.session.execute(
            text(
                "WITH changed AS ("
                "UPDATE catalog_media m SET projection_revision=m.projection_revision+:step "
                "WHERE EXISTS (SELECT 1 FROM catalog_memberships membership "
                "WHERE membership.media_id=m.id AND membership.ended_at IS NULL "
                "AND membership.group_id=ANY(:groups)) "
                "RETURNING m.id, m.revision, m.projection_revision, m.published_at, m.kind"
                "), events AS MATERIALIZED ("
                "SELECT gen_random_uuid() AS event_id, changed.*, "
                "g.id AS group_id, g.revision AS group_revision, g.composition_revision, "
                "destinations.target "
                "FROM changed JOIN catalog_memberships membership "
                "ON membership.media_id=changed.id AND membership.ended_at IS NULL "
                "JOIN catalog_groups g ON g.id=membership.group_id "
                "CROSS JOIN unnest(CAST(:targets AS text[])) destinations(target)"
                ") INSERT INTO catalog_outbox "
                "(id, target, payload, created_at, ready_at, attempts, "
                "claim_id, claimed_until, delivered_at) "
                "SELECT event_id, target, jsonb_build_object("
                "'event_id', event_id, 'media_id', id, 'group_id', group_id, "
                "'group_revision', group_revision, 'composition_revision', composition_revision, "
                "'revision', projection_revision, 'source_revision', revision, "
                "'published_at', published_at, 'media_kind', kind, 'active', true), "
                ":now, :now, :attempts, NULL, NULL, NULL FROM events"
            ),
            {
                "groups": list(group_ids),
                "targets": [
                    target.value
                    for target in delivery_targets(self.session, DeliveryTarget.SEARCH_STATE)
                ],
                "step": Number.ONE,
                "attempts": Number.ZERO,
                "now": now,
            },
        )

    async def limit_vote_rate(self, voter_id: UUID, *, maximum: int, now: datetime) -> None:
        """Проверить общий лимит действий с PostgreSQL-блокировкой."""
        await self.session.execute(
            insert(VoteRateRow)
            .values(user_id=voter_id, window=now, count=Number.ZERO)
            .on_conflict_do_nothing(index_elements=[VoteRateRow.user_id])
        )
        row = await self.session.scalar(
            select(VoteRateRow).where(VoteRateRow.user_id == voter_id).with_for_update()
        )
        if row is None:
            raise ApplicationError(ErrorCode.UNAVAILABLE)
        if row.window + timedelta(seconds=CatalogDefault.MINUTE_SECONDS) <= now:
            row.window = now
            row.count = Number.ZERO
        if row.count >= maximum:
            raise ApplicationError(ErrorCode.RATE_LIMITED)
        row.count += Number.ONE

    async def dismiss(self, dismissal: SuggestionDismissal) -> None:
        """Сохранить идемпотентное личное отклонение."""
        await self.session.execute(
            insert(DismissalRow)
            .values(
                id=dismissal.id,
                user_id=dismissal.user_id,
                media_id=dismissal.source_media_id,
                group_id=dismissal.candidate_group.id,
                group_revision=dismissal.candidate_group.composition_revision.value,
                dismissed_at=dismissal.dismissed_at,
            )
            .on_conflict_do_nothing()
        )
