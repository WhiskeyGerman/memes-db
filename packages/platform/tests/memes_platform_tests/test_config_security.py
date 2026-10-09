import pytest
from pydantic import ValidationError

from memes_platform.infrastructure.configuration import BaseServiceSettings


def test_service_field_error_hides_short_internal_secret(
    service_environment: None, configuration_markers: tuple[str, str]
) -> None:
    marker = configuration_markers[0]
    with pytest.raises(ValidationError) as rejected:
        BaseServiceSettings(internal_api_key=marker)
    assert marker not in str(rejected.value)
