"""Точечная редакция credentials с сохранением адресов ресурсов и контекста."""

import re
from collections.abc import Mapping
from enum import StrEnum
from urllib.parse import quote, quote_plus, unquote, unquote_plus


class RedactionText(StrEnum):
    """Маркеры и форматы явно чувствительных данных."""

    REDACTED = "[REDACTED]"
    URL = r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"]+"
    AUTHORITY = r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<authority>[^/?#]*)(?P<tail>.*)"
    KEY_PARTS = r"[.\[\]-]+"
    ENVIRONMENT_CREDENTIAL_PATTERN = (
        r"(?:^|_)(?:PASSWORD|PASSWD|TOKEN|SECRET|SECRET_KEY|API_KEY|ACCESS_KEY|ACCESS_KEY_ID)$"
    )
    ENVIRONMENT_DSN = r"(?:^|_)(?:DATABASE_URL|DATABASE_DSN|DSN)$"
    ASSIGNMENT = (
        r"(?P<prefix>\b(?P<key>[\w.%+\[\]-]+)[\"']?\s*[:=]\s*)"
        r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
        r"(?:Bearer|Basic)[ \t]+[^\s,;&]+|[^\s,;&]+)"
    )
    BEARER = r"\b(?P<scheme>Bearer)[ \t]+[a-zA-Z0-9._~+/-]+=*"


SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "x_api_key",
        "access_key",
        "access_key_id",
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "session_id",
        "sessionid",
        "signature",
        "sig",
        "awsaccesskeyid",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
        "x_goog_credential",
        "x_goog_signature",
    }
)
URL_PATTERN = re.compile(RedactionText.URL)
AUTHORITY_PATTERN = re.compile(RedactionText.AUTHORITY)
KEY_PARTS = re.compile(RedactionText.KEY_PARTS)
ENVIRONMENT_SECRET = re.compile(RedactionText.ENVIRONMENT_CREDENTIAL_PATTERN, re.IGNORECASE)
ENVIRONMENT_DSN = re.compile(RedactionText.ENVIRONMENT_DSN, re.IGNORECASE)
ASSIGNMENT = re.compile(RedactionText.ASSIGNMENT, re.IGNORECASE)
BEARER = re.compile(RedactionText.BEARER, re.IGNORECASE)


def sensitive_key(key: str) -> bool:
    """Определить чувствительное поле по имени, а не по произвольной подстроке.

    Args:
        key: Имя поля, включая percent-encoded или вложенную форму.

    Returns:
        bool: Поле содержит credentials, подпись или идентификатор сессии.
    """
    normalized = KEY_PARTS.sub("_", unquote_plus(key).casefold()).strip("_")
    return (
        normalized in SENSITIVE_KEYS
        or normalized.rsplit("_", 1)[-1]
        in {"password", "passwd", "pwd", "token", "secret", "signature"}
        or any(part in SENSITIVE_KEYS for part in KEY_PARTS.split(unquote_plus(key).casefold()))
    )


def environment_secrets(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Извлечь секреты окружения и пароли DSN, сохранив адреса подключений.

    Args:
        environment: Переменные окружения процесса.

    Returns:
        tuple[str, ...]: Непустые credentials; целые DSN в список не попадают.
    """
    values: set[str] = set()
    for name, value in environment.items():
        if not value:
            continue
        if ENVIRONMENT_SECRET.search(name):
            values.add(value)
        elif ENVIRONMENT_DSN.search(name):
            authority = AUTHORITY_PATTERN.match(value)
            if authority is not None:
                userinfo, separator, _ = authority["authority"].rpartition("@")
                if separator:
                    _, password_separator, password = userinfo.partition(":")
                    if password_separator and password:
                        values.update((password, unquote(password)))
    return tuple(sorted(values, key=len, reverse=True))


class LogSanitizer:
    """Редактировать секретные значения без уничтожения полезных URL и сообщений."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        """Сохранить известные credentials вместе с URL-encoded вариантами.

        Args:
            secrets: Реальные секреты текущего процесса.
        """
        variants = {
            variant
            for secret in secrets
            if secret
            for variant in (secret, quote(secret, safe=""), quote_plus(secret))
        }
        self.secrets = tuple(sorted(variants, key=len, reverse=True))

    def redact_values(self, value: str) -> str:
        """Заменить только известные секретные значения.

        Args:
            value: Текст или компонент URI.

        Returns:
            str: Текст с маркерами вместо известных credentials.
        """
        for secret in self.secrets:
            value = value.replace(secret, RedactionText.REDACTED)
        return value

    def redact_query(self, query: str) -> str:
        """Сохранить порядок, дубликаты и кодирование безопасных URL-параметров.

        Args:
            query: Query string или OAuth fragment без начального разделителя.

        Returns:
            str: Исходная строка с заменой только чувствительных значений.
        """
        parameters: list[str] = []
        for parameter in query.split("&"):
            key, separator, value = parameter.partition("=")
            if not separator:
                parameters.append(self.redact_values(parameter))
                continue
            if separator and sensitive_key(key):
                value = RedactionText.REDACTED
            else:
                decoded = unquote_plus(value)
                safe = self.redact_values(decoded)
                if safe != decoded:
                    value = quote_plus(safe, safe="[]")
            parameters.append(f"{key}{separator}{value}")
        return "&".join(parameters)

    def redact_url(self, value: str) -> str:
        """Сохранить адрес ресурса, заменив userinfo и секретные параметры.

        Args:
            value: Абсолютный URL, в том числе диагностический некорректный URL.

        Returns:
            str: URL с исходными hostname, портом, путём и безопасными параметрами.
        """
        if value in self.secrets:
            return RedactionText.REDACTED
        authority = AUTHORITY_PATTERN.fullmatch(value)
        if authority is None:
            return self.redact_values(value)
        netloc = authority["authority"]
        _, separator, host = netloc.rpartition("@")
        if separator:
            netloc = f"{RedactionText.REDACTED}@{host}"
        path_and_query, fragment_separator, fragment = authority["tail"].partition("#")
        path, query_separator, query = path_and_query.partition("?")
        return (
            f"{authority['scheme']}{self.redact_values(netloc)}{self.redact_values(path)}"
            f"{query_separator}{self.redact_query(query)}"
            f"{fragment_separator}{self.redact_query(fragment)}"
        )

    def redact_assignment(self, match: re.Match[str]) -> str:
        """Заменить значение явно чувствительного поля обычного текста.

        Args:
            match: Ключ и значение с исходным разделителем.

        Returns:
            str: Исходное поле или отредактированное значение.
        """
        if not sensitive_key(match["key"]):
            return match[0]
        value = match["value"]
        if value.startswith(('"', "'")):
            marker = f"{value[0]}{RedactionText.REDACTED}{value[0]}"
        else:
            marker = RedactionText.REDACTED
        return f"{match['prefix']}{marker}"

    def redact_text(self, value: str) -> str:
        """Отредактировать credentials вне URL, сохранив остальную диагностику.

        Args:
            value: Фрагмент сообщения без абсолютных URL.

        Returns:
            str: Текст с сохранением безопасных полей.
        """
        value = ASSIGNMENT.sub(self.redact_assignment, value)
        value = BEARER.sub(lambda match: f"{match['scheme']} {RedactionText.REDACTED}", value)
        return self.redact_values(value)

    def redact(self, value: str) -> str:
        """Обработать URL и обычный текст раздельно, сохранив границы компонентов.

        Args:
            value: Сообщение, traceback или разрешённое строковое поле.

        Returns:
            str: Диагностический текст с точечной редакцией credentials.
        """
        # Секретное поле может содержать целый URL. При этом назначения внутри
        # самого URL обрабатываются по компонентам, без потери fragment и query.
        urls = tuple((match.start(), match.end()) for match in URL_PATTERN.finditer(value))
        value = ASSIGNMENT.sub(
            lambda match: (
                match[0]
                if any(start <= match.start() < end for start, end in urls)
                else self.redact_assignment(match)
            ),
            value,
        )
        parts: list[str] = []
        end = 0
        for match in URL_PATTERN.finditer(value):
            parts.append(self.redact_text(value[end : match.start()]))
            url = match[0].rstrip("'")
            parts.append(self.redact_url(url) + match[0][len(url) :])
            end = match.end()
        parts.append(self.redact_text(value[end:]))
        return "".join(parts)
