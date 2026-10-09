"""Отпечатки индивидуальных машинных секретов."""

from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe

from memes_processing.application.ports.security import IDeviceCredentials


class DeviceCredentials(IDeviceCredentials):
    """Высокоэнтропийный секрет не требует хранения обратимого токена."""

    def issue(self) -> tuple[str, str]:
        """Создать отдельный секрет с 256 битами энтропии."""
        token = token_urlsafe(32)
        return token, sha256(token.encode()).hexdigest()

    def verify(self, token: str, token_hash: str) -> bool:
        """Сравнить отпечатки за постоянное время."""
        return compare_digest(sha256(token.encode()).hexdigest(), token_hash)
