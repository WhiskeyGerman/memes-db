import pytest
from pydantic import ValidationError

from memes_catalog.infrastructure.configuration import CatalogSettings


def test_service_model_error_hides_entire_secret_input(
    service_environment: None, configuration_markers: tuple[str, str]
) -> None:
    marker = configuration_markers[1]
    with pytest.raises(ValidationError) as rejected:
        CatalogSettings(internal_api_key=marker, active_search_index="shadow")
    assert marker not in str(rejected.value)
