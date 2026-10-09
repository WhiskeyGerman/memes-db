"""PostgreSQL-адаптер учётных записей."""

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.application.ports.users import IUserRepository
from memes_identity.domain.user import Login, User, UserId, UserRole, UserState
from memes_identity.infrastructure.persistence.models import UserRow
from memes_kernel.values import Revision


def map_user(row: UserRow) -> User:
    """Восстановить доменное состояние из ORM-записи.

    Args:
        row: Запись PostgreSQL.

    Returns:
        User: Проверенная бизнес-модель.
    """
    return User(
        id=UserId(row.id),
        login=Login(value=row.login) if row.login is not None else None,
        state=UserState(row.state),
        roles=frozenset(UserRole(role) for role in row.roles),
        revision=Revision(value=row.revision),
        created_at=row.created_at,
        deletion_requested_at=row.deletion_requested_at,
        deleted_at=row.deleted_at,
        delete_media=row.delete_media,
    )


class SqlUserRepository(IUserRepository):
    """Репозиторий с уникальным логином и сравнением ревизии в SQL."""

    def __init__(self, session: AsyncSession) -> None:
        """Получить транзакционную сессию.

        Args:
            session: Текущая транзакция.
        """
        self._session = session

    async def get(self, user_id: UserId, *, lock: bool = False) -> User | None:
        """Найти аккаунт с необязательной блокировкой.

        Args:
            user_id: Идентификатор.
            lock: Удержать блокировку строки.
        """
        statement = select(UserRow).where(UserRow.id == user_id)
        if lock:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement.execution_options(populate_existing=True))
        return map_user(row) if row is not None else None

    async def batch(self, user_ids: tuple[UserId, ...]) -> tuple[User, ...]:
        """Прочитать авторов одним индексируемым IN-запросом."""
        rows = await self._session.scalars(select(UserRow).where(UserRow.id.in_(user_ids)))
        return tuple(map_user(row) for row in rows)

    async def find_by_login(self, login: Login) -> User | None:
        """Найти аккаунт по нормализованному уникальному логину.

        Args:
            login: Нормализованный логин.
        """
        row = await self._session.scalar(select(UserRow).where(UserRow.login == login.value))
        return map_user(row) if row is not None else None

    async def save(self, user: User, expected_revision: Revision | None) -> None:
        """Записать доменное состояние с проверкой ревизии.

        Args:
            user: Новое бизнес-состояние.
            expected_revision: Прежняя ревизия либо создание.

        Raises:
            IdentityError: Конфликт логина либо параллельное изменение.
        """
        values = {
            "login": user.login.value if user.login is not None else None,
            "state": user.state.value,
            "roles": [role.value for role in sorted(user.roles)],
            "revision": user.revision.value,
            "created_at": user.created_at,
            "deletion_requested_at": user.deletion_requested_at,
            "deleted_at": user.deleted_at,
            "delete_media": user.delete_media,
        }
        try:
            if expected_revision is None:
                self._session.add(UserRow(id=user.id, **values))
                await self._session.flush()
            else:
                saved = await self._session.scalar(
                    update(UserRow)
                    .where(UserRow.id == user.id, UserRow.revision == expected_revision.value)
                    .values(**values)
                    .returning(UserRow.id)
                )
                if saved is None:
                    raise IdentityError(IdentityFailure.CONFLICT)
        except IntegrityError as error:
            raise IdentityError(IdentityFailure.LOGIN_OCCUPIED) from error
