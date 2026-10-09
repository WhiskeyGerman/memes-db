"""Внешние JSON-контракты Identity без ORM-моделей."""

from datetime import datetime
from enum import IntEnum, StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from memes_identity.domain.user import UserRole, UserState


class ApiLimit(IntEnum):
    """Границы размера пользовательского запроса."""

    PASSWORD_MAXIMUM = 128
    LOGIN_MAXIMUM = 128
    TOKEN_MAXIMUM = 256
    BATCH_MAXIMUM = 100


class TokenType(StrEnum):
    """Схема предъявления access token."""

    BEARER = "Bearer"


class RequestSchema(BaseModel):
    """Закрытый набор допустимых входных полей."""

    model_config = ConfigDict(extra="forbid")


class CredentialsRequest(RequestSchema):
    """Регистрация либо вход с исходным паролем."""

    login: str = Field(max_length=ApiLimit.LOGIN_MAXIMUM)
    password: SecretStr = Field(max_length=ApiLimit.PASSWORD_MAXIMUM)


class AccessResponse(BaseModel):
    """Access token; refresh передаётся исключительно в защищённом cookie."""

    access_token: str = Field(repr=False)
    token_type: TokenType = TokenType.BEARER
    access_expires_at: datetime


class IntrospectionRequest(RequestSchema):
    """Непрозрачный токен для доверенного межсервисного запроса."""

    token: SecretStr = Field(min_length=1, max_length=ApiLimit.TOKEN_MAXIMUM)


class IntrospectionResponse(BaseModel):
    """Минимальная проверенная идентичность без секретов."""

    user_id: UUID
    login: str
    roles: list[UserRole]


class UserResponse(BaseModel):
    """Безопасная проекция пользователя либо удалённого аккаунта."""

    user_id: UUID
    login: str | None
    roles: list[UserRole]
    state: UserState
    delete_media: bool


class UserBatchRequest(RequestSchema):
    """Ограниченная пачка идентификаторов авторов."""

    user_ids: list[UUID] = Field(max_length=ApiLimit.BATCH_MAXIMUM)


class UserBatchResponse(BaseModel):
    """Проекции найденных авторов, включая обезличенные tombstone."""

    users: list[UserResponse]


class ChangeLoginRequest(RequestSchema):
    """Смена логина с подтверждением пароля."""

    login: str = Field(max_length=ApiLimit.LOGIN_MAXIMUM)
    password: SecretStr = Field(max_length=ApiLimit.PASSWORD_MAXIMUM)


class ChangePasswordRequest(RequestSchema):
    """Смена пароля с подтверждением текущего."""

    password: SecretStr = Field(max_length=ApiLimit.PASSWORD_MAXIMUM)
    new_password: SecretStr = Field(max_length=ApiLimit.PASSWORD_MAXIMUM)


class DeleteAccountRequest(RequestSchema):
    """Явный выбор сохранения либо удаления собственных медиа."""

    password: SecretStr = Field(max_length=ApiLimit.PASSWORD_MAXIMUM)
    delete_media: bool = False


class ChangeAccessRequest(RequestSchema):
    """Операторское изменение состояния и ролей."""

    blocked: bool
    roles: list[UserRole]


class ErrorResponse(BaseModel):
    """Машинный код отказа без внутренних сообщений и секретов."""

    code: str
