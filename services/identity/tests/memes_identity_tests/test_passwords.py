import pytest

from memes_identity.infrastructure.authentication.passwords import Argon2PasswordVerifier
from memes_identity.infrastructure.configuration import IdentitySettings


@pytest.mark.asyncio
async def test_argon2id_is_salted_preserves_unicode_and_whitespace(
    identity_settings: IdentitySettings,
) -> None:
    verifier = Argon2PasswordVerifier(identity_settings)
    password = "  проверочный пароль  "  # noqa: S105
    first = await verifier.encode(password)
    second = await verifier.encode(password)
    assert first.startswith("$argon2id$")
    assert first != second
    assert await verifier.verify(password, first)
    assert not await verifier.verify(password.strip(), first)
    assert not await verifier.verify(password, None)
    assert not await verifier.verify(password, "malformed-verifier")
