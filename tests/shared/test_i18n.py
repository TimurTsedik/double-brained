from enum import StrEnum

import pytest

from second_brain.shared.i18n import (
    DEFAULT_LOCALE,
    Locale,
    is_language_chosen,
    resolve_locale,
)


def test_locale_is_a_string_enum_with_exactly_ru_and_en() -> None:
    assert issubclass(Locale, StrEnum)
    assert Locale.RU.value == "ru"
    assert Locale.EN.value == "en"
    assert {member.value for member in Locale} == {"ru", "en"}


def test_default_locale_is_russian() -> None:
    assert DEFAULT_LOCALE is Locale.RU


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ru", Locale.RU),
        ("en", Locale.EN),
        (None, Locale.RU),
        ("", Locale.RU),
        ("de", Locale.RU),
        ("  ", Locale.RU),
        ("RU", Locale.RU),
    ],
)
def test_resolve_locale_maps_known_codes_and_defaults_everything_else(
    value: str | None, expected: Locale
) -> None:
    assert resolve_locale(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ru", True),
        ("en", True),
        (None, False),
        ("", False),
        ("de", False),
        ("  ", False),
        ("RU", False),
    ],
)
def test_is_language_chosen_only_for_valid_codes(
    value: str | None, expected: bool
) -> None:
    assert is_language_chosen(value) is expected
