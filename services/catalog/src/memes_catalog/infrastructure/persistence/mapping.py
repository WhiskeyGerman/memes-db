"""Явное преобразование записей ORM в immutable Domain."""

from datetime import timedelta
from enum import StrEnum
from fractions import Fraction
from typing import cast
from uuid import UUID

from memes_catalog.application.dto.catalog import Upload, UploadState
from memes_catalog.domain.groups import (
    DetachTarget,
    GroupId,
    GroupMembership,
    GroupRef,
    GroupState,
    MembershipEnd,
    MembershipId,
    MergeTarget,
    VariationGroup,
)
from memes_catalog.domain.media import (
    AnimationMetadata,
    AudioCodec,
    AudioMetadata,
    AuthorId,
    Dimensions,
    ImageMetadata,
    Media,
    MediaFormat,
    MediaId,
    MediaKind,
    MediaState,
    OriginalMedia,
    VideoCodec,
    VideoMetadata,
)
from memes_catalog.domain.voting import (
    BallotId,
    BallotState,
    DecisionId,
    VariationBallot,
    VariationTarget,
    VariationVote,
    VoteId,
    VotingRule,
)
from memes_catalog.infrastructure.persistence.models import (
    BallotRow,
    GroupRow,
    MediaRow,
    MembershipRow,
    UploadRow,
    VoteRow,
)
from memes_kernel.values import FileRef, Revision, Sha256
from memes_platform.application.dto.json import JsonObject


class MetadataField(StrEnum):
    """Ключи сериализованных технических фактов."""

    DIMENSIONS = "dimensions"
    WIDTH = "width"
    HEIGHT = "height"
    DURATION = "duration_ms"
    FRAMES = "frames"
    CODEC = "codec"
    CHANNELS = "channels"
    SAMPLE_RATE = "sample_rate_hz"
    BIT_DEPTH = "bit_depth"
    AUDIO = "audio"
    FRAME_RATE = "frame_rate"
    LEVEL = "level"


class TargetField(StrEnum):
    """Ключи сохранённой доменной цели."""

    KIND = "kind"
    MEMBERSHIP = "membership_id"
    FIRST = "first_id"
    FIRST_REVISION = "first_revision"
    SECOND = "second_id"
    SECOND_REVISION = "second_revision"
    MERGE = "merge"
    DETACH = "detach"


def dimensions(data: JsonObject) -> Dimensions:
    """Восстановить размеры."""
    raw = cast(JsonObject, data[MetadataField.DIMENSIONS])
    return Dimensions(
        width=cast(int, raw[MetadataField.WIDTH]), height=cast(int, raw[MetadataField.HEIGHT])
    )


def audio(data: JsonObject) -> AudioMetadata:
    """Восстановить факты аудио."""
    return AudioMetadata(
        codec=AudioCodec(cast(str, data[MetadataField.CODEC])),
        duration=timedelta(milliseconds=cast(int, data[MetadataField.DURATION])),
        channels=cast(int, data[MetadataField.CHANNELS]),
        sample_rate_hz=cast(int, data[MetadataField.SAMPLE_RATE]),
        bit_depth=cast(int | None, data.get(MetadataField.BIT_DEPTH)),
    )


def original(
    *, file: FileRef, media_format: MediaFormat, kind: MediaKind, data: JsonObject
) -> OriginalMedia:
    """Восстановить проверенное содержимое.

    Args:
        file: Идентичность байтов.
        media_format: Формат.
        kind: Вид.
        data: Сохранённые факты.
    """
    if kind is MediaKind.IMAGE:
        metadata = ImageMetadata(dimensions=dimensions(data))
        return OriginalMedia(file=file, format=media_format, metadata=metadata)
    if kind is MediaKind.AUDIO:
        return OriginalMedia(file=file, format=media_format, metadata=audio(data))
    if kind is MediaKind.ANIMATION:
        return OriginalMedia(
            file=file,
            format=media_format,
            metadata=AnimationMetadata(
                dimensions=dimensions(data),
                duration=timedelta(milliseconds=cast(int, data[MetadataField.DURATION])),
                frames=cast(int, data[MetadataField.FRAMES]),
            ),
        )
    raw_audio = data.get(MetadataField.AUDIO)
    raw_level = data.get(MetadataField.LEVEL)
    return OriginalMedia(
        file=file,
        format=media_format,
        metadata=VideoMetadata(
            dimensions=dimensions(data),
            codec=VideoCodec(cast(str, data[MetadataField.CODEC])),
            duration=timedelta(milliseconds=cast(int, data[MetadataField.DURATION])),
            frames=cast(int, data[MetadataField.FRAMES]),
            frame_rate=Fraction(cast(str, data[MetadataField.FRAME_RATE])),
            audio=audio(cast(JsonObject, raw_audio)) if raw_audio is not None else None,
            level=Fraction(cast(str, raw_level)) if raw_level is not None else None,
        ),
    )


def media(row: MediaRow) -> Media:
    """Восстановить публикацию."""
    file = FileRef(id=row.file_id, sha256=Sha256(value=row.sha256), size_bytes=row.size_bytes)
    return Media(
        id=MediaId(row.id),
        revision=Revision(value=row.revision),
        author_id=AuthorId(row.author_id),
        original=original(
            file=file,
            media_format=MediaFormat(row.format),
            kind=MediaKind(row.kind),
            data=row.metadata_payload,
        ),
        published_at=row.published_at,
        state=MediaState(row.state),
        deleted_at=row.deleted_at,
    )


def group(row: GroupRow) -> VariationGroup:
    """Восстановить состав."""
    return VariationGroup(
        id=GroupId(row.id),
        revision=Revision(value=row.revision),
        member_count=row.member_count,
        composition_revision=Revision(value=row.composition_revision),
        created_at=row.created_at,
        state=GroupState(row.state),
        successor_id=GroupId(row.successor_id) if row.successor_id else None,
    )


def membership(row: MembershipRow) -> GroupMembership:
    """Восстановить эпизод."""
    return GroupMembership(
        id=MembershipId(row.id),
        revision=Revision(value=row.revision),
        media_id=MediaId(row.media_id),
        group_id=GroupId(row.group_id),
        joined_at=row.joined_at,
        ended_at=row.ended_at,
        end_reason=MembershipEnd(row.end_reason) if row.end_reason else None,
    )


def upload(row: UploadRow) -> Upload:
    """Восстановить временную передачу."""
    return Upload(
        id=row.id,
        author_id=row.author_id,
        file_id=row.file_id,
        media_id=row.media_id,
        object_key=row.object_key,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
        expires_at=row.expires_at,
        state=UploadState(row.state),
        claimed_until=row.claimed_until,
        outcome_media_id=row.outcome_media_id,
        claim_id=row.claim_id,
    )


def target_payload(target: VariationTarget) -> JsonObject:
    """Сериализовать каноническую доменную цель."""
    if isinstance(target, DetachTarget):
        return {
            TargetField.KIND: TargetField.DETACH,
            TargetField.MEMBERSHIP: str(target.membership_id),
        }
    return {
        TargetField.KIND: TargetField.MERGE,
        TargetField.FIRST: str(target.first.id),
        TargetField.FIRST_REVISION: target.first.composition_revision.value,
        TargetField.SECOND: str(target.second.id),
        TargetField.SECOND_REVISION: target.second.composition_revision.value,
    }


def target(data: JsonObject) -> VariationTarget:
    """Восстановить цель."""
    if data[TargetField.KIND] == TargetField.DETACH:
        return DetachTarget(
            membership_id=MembershipId(UUID(cast(str, data[TargetField.MEMBERSHIP])))
        )
    return MergeTarget(
        first=GroupRef(
            id=GroupId(UUID(cast(str, data[TargetField.FIRST]))),
            composition_revision=Revision(value=cast(int, data[TargetField.FIRST_REVISION])),
        ),
        second=GroupRef(
            id=GroupId(UUID(cast(str, data[TargetField.SECOND]))),
            composition_revision=Revision(value=cast(int, data[TargetField.SECOND_REVISION])),
        ),
    )


def ballot(row: BallotRow) -> VariationBallot:
    """Восстановить голосование."""
    return VariationBallot(
        id=BallotId(row.id),
        revision=Revision(value=row.revision),
        target=target(row.target_payload),
        rule=VotingRule(threshold=row.threshold, policy_revision=row.policy_revision),
        opened_at=row.opened_at,
        active_votes=row.active_votes,
        state=BallotState(row.state),
        closed_at=row.closed_at,
        decision_id=DecisionId(row.decision_id) if row.decision_id else None,
    )


def vote(row: VoteRow) -> VariationVote:
    """Восстановить голос."""
    return VariationVote(
        id=VoteId(row.id),
        revision=Revision(value=row.revision),
        ballot_id=BallotId(row.ballot_id),
        voter_id=AuthorId(row.voter_id),
        cast_at=row.cast_at,
        withdrawn_at=row.withdrawn_at,
    )
