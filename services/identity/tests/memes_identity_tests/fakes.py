"""Тестовые порты Identity; production использует только PostgreSQL."""

import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from memes_identity.application.dto.authentication import (
    AccountDeleted,
    AuthenticatedActor,
    IssuedTokens,
    SessionRotation,
)
from memes_identity.application.dto.outbox import (
    AccountDeletionDelivery,
    DeletionDestination,
    DeletionRecipient,
)
from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.application.ports.clock import IClock
from memes_identity.application.ports.outbox import IAccountDeletionPublisher, IAccountOutbox
from memes_identity.application.ports.passwords import ICredentialRepository, IPasswordVerifier
from memes_identity.application.ports.rate_limit import ILoginRateLimiter
from memes_identity.application.ports.sessions import ISessionService
from memes_identity.application.ports.unit_of_work import IIdentityUnitOfWork
from memes_identity.application.ports.users import IUserRepository
from memes_identity.domain.user import Login, User, UserId, UserState
from memes_kernel.values import Number, Revision
from memes_platform.application.dto.json import JsonObject
from memes_platform.application.errors import ApplicationError, ErrorCode
from memes_platform.application.ports.deletion_journal import IDeletionJournal


@dataclass
class MemoryState:
    users: dict[UserId, User] = field(default_factory=dict)
    credentials: dict[UserId, str] = field(default_factory=dict)
    sessions: dict[UUID, tuple[UserId, IssuedTokens]] = field(default_factory=dict)
    consumed: set[str] = field(default_factory=set)
    revoked: set[UUID] = field(default_factory=set)
    outbox: list[AccountDeleted] = field(default_factory=list)
    attempts: int = Number.ZERO
    allowed: bool = True


class FixedClock(IClock):
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class FakePasswords(IPasswordVerifier):
    def __init__(self) -> None:
        self.verified: list[tuple[str, str | None]] = []

    async def encode(self, password: str) -> str:
        return password[::-1]

    async def verify(self, password: str, verifier: str | None) -> bool:
        self.verified.append((password, verifier))
        return verifier is not None and password[::-1] == verifier


class FakeUsers(IUserRepository):
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    async def get(self, user_id: UserId, *, lock: bool = False) -> User | None:
        return self.state.users.get(user_id)

    async def batch(self, user_ids: tuple[UserId, ...]) -> tuple[User, ...]:
        return tuple(
            self.state.users[identifier]
            for identifier in user_ids
            if identifier in self.state.users
        )

    async def find_by_login(self, login: Login) -> User | None:
        return next((user for user in self.state.users.values() if user.login == login), None)

    async def save(self, user: User, expected_revision: Revision | None) -> None:
        existing = self.state.users.get(user.id)
        if existing is not None and existing.revision != expected_revision:
            raise IdentityError(IdentityFailure.CONFLICT)
        if any(
            other.id != user.id and other.login is not None and other.login == user.login
            for other in self.state.users.values()
        ):
            raise IdentityError(IdentityFailure.LOGIN_OCCUPIED)
        self.state.users[user.id] = user


class FakeCredentials(ICredentialRepository):
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    async def get(self, user_id: UserId) -> str | None:
        return self.state.credentials.get(user_id)

    async def set(self, user_id: UserId, verifier: str, now: datetime) -> None:
        self.state.credentials[user_id] = verifier

    async def delete(self, user_id: UserId) -> None:
        self.state.credentials.pop(user_id, None)


class FakeSessions(ISessionService):
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    async def issue(self, user_id: UserId, now: datetime) -> IssuedTokens:
        tokens = IssuedTokens(
            access_token=str(uuid4()),
            refresh_token=str(uuid4()),
            access_expires_at=now + timedelta(minutes=15),
            refresh_expires_at=now + timedelta(days=30),
            session_id=uuid4(),
        )
        self.state.sessions[tokens.session_id] = (user_id, tokens)
        return tokens

    async def authenticate(self, access_token: str, now: datetime) -> AuthenticatedActor | None:
        for session_id, (user_id, tokens) in self.state.sessions.items():
            user = self.state.users[user_id]
            if (
                tokens.access_token == access_token
                and tokens.access_expires_at > now
                and session_id not in self.state.revoked
                and user.state is UserState.ACTIVE
                and user.login is not None
            ):
                return AuthenticatedActor(
                    user_id=user_id,
                    login=user.login.value,
                    roles=user.roles,
                    session_id=session_id,
                )
        return None

    async def rotate(self, refresh_token: str, now: datetime) -> SessionRotation:
        for session_id, (user_id, tokens) in self.state.sessions.items():
            if tokens.refresh_token != refresh_token:
                continue
            if refresh_token in self.state.consumed:
                self.state.revoked.add(session_id)
                return SessionRotation(failure=IdentityFailure.REFRESH_REPLAY)
            if session_id in self.state.revoked:
                return SessionRotation(failure=IdentityFailure.UNAUTHORIZED)
            self.state.consumed.add(refresh_token)
            return SessionRotation(tokens=await self.issue(user_id, now))
        return SessionRotation(failure=IdentityFailure.UNAUTHORIZED)

    async def revoke_all(
        self, user_id: UserId, now: datetime, *, except_session_id: UUID | None = None
    ) -> None:
        self.state.revoked.update(
            session_id
            for session_id, (owner, _) in self.state.sessions.items()
            if owner == user_id and session_id != except_session_id
        )

    async def revoke(self, session_id: UUID, now: datetime) -> None:
        self.state.revoked.add(session_id)

    async def delete(self, user_id: UserId) -> None:
        for session_id in tuple(self.state.sessions):
            if self.state.sessions[session_id][Number.ZERO] == user_id:
                del self.state.sessions[session_id]


class FakeRateLimiter(ILoginRateLimiter):
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    async def consume(self, *, account: str, peer_ip: str, now: datetime) -> bool:
        self.state.attempts += Number.ONE
        return self.state.allowed


class FakeOutbox(IAccountOutbox):
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    async def add(
        self, event: AccountDeleted, *, destination: DeletionDestination | None = None
    ) -> None:
        if all(existing.event_id != event.event_id for existing in self.state.outbox):
            self.state.outbox.append(event)

    async def claim(
        self, now: datetime, *, destination: DeletionDestination | None = None
    ) -> AccountDeletionDelivery | None:
        if not self.state.outbox:
            return None
        return AccountDeletionDelivery(
            delivery_id=uuid4(),
            event=self.state.outbox[Number.ZERO],
            destination=destination
            or DeletionDestination(recipient=DeletionRecipient.CATALOG, base_url="http://catalog"),
            claim_token=uuid4(),
        )

    async def acknowledge(self, delivery: AccountDeletionDelivery, now: datetime) -> None:
        self.state.outbox.remove(delivery.event)

    async def postpone(self, delivery: AccountDeletionDelivery, now: datetime) -> None:
        return None

    async def pending(self, destination: DeletionDestination) -> int:
        return len(self.state.outbox)


class FakeUnitOfWork(IIdentityUnitOfWork):
    def __init__(self, state: MemoryState) -> None:
        self.state = state
        self._snapshot: MemoryState | None = None
        self.commits: int = Number.ZERO
        self.fail_next_commit = False

    @property
    def users(self) -> IUserRepository:
        return FakeUsers(self.state)

    @property
    def credentials(self) -> ICredentialRepository:
        return FakeCredentials(self.state)

    @property
    def sessions(self) -> ISessionService:
        return FakeSessions(self.state)

    @property
    def rate_limiter(self) -> ILoginRateLimiter:
        return FakeRateLimiter(self.state)

    @property
    def outbox(self) -> IAccountOutbox:
        return FakeOutbox(self.state)

    async def __aenter__(self) -> Self:
        self._snapshot = deepcopy(self.state)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._snapshot is not None:
            self.state.users = self._snapshot.users
            self.state.credentials = self._snapshot.credentials
            self.state.sessions = self._snapshot.sessions
            self.state.consumed = self._snapshot.consumed
            self.state.revoked = self._snapshot.revoked
            self.state.outbox = self._snapshot.outbox
            self.state.attempts = self._snapshot.attempts
        self._snapshot = None

    async def commit(self) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise IdentityError(IdentityFailure.CONFLICT)
        self._snapshot = None
        self.commits += Number.ONE


class MemoryDeletionJournal(IDeletionJournal):
    def __init__(self) -> None:
        self.events: list[JsonObject] = []
        self.available = True

    async def record(self, event: JsonObject) -> None:
        if not self.available:
            raise ApplicationError(ErrorCode.UNAVAILABLE)
        self.events.append(deepcopy(event))

    async def records(self, *, since: datetime | None = None) -> AsyncIterator[JsonObject]:
        for event in tuple(self.events):
            yield deepcopy(event)


class PausingPasswords(IPasswordVerifier):
    def __init__(self, verifier: IPasswordVerifier) -> None:
        self.delegate = verifier
        self.checked = asyncio.Event()
        self.resume = asyncio.Event()

    async def encode(self, password: str) -> str:
        return await self.delegate.encode(password)

    async def verify(self, password: str, verifier: str | None) -> bool:
        result = await self.delegate.verify(password, verifier)
        self.checked.set()
        await self.resume.wait()
        return result


class RecordingDeletionPublisher(IAccountDeletionPublisher):
    def __init__(self) -> None:
        self.calls: list[AccountDeletionDelivery] = []
        self.rejected: set[DeletionRecipient] = set()
        self.pause = False
        self.started = asyncio.Event()
        self.resume = asyncio.Event()

    async def publish(self, delivery: AccountDeletionDelivery) -> bool:
        self.calls.append(delivery)
        self.started.set()
        if self.pause:
            await self.resume.wait()
        return delivery.destination.recipient not in self.rejected
