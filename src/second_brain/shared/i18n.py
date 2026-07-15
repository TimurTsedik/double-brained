from enum import StrEnum


class Locale(StrEnum):
    RU = "ru"
    EN = "en"


DEFAULT_LOCALE = Locale.RU


def resolve_locale(language: str | None) -> Locale:
    if language in _CODE_TO_LOCALE:
        return _CODE_TO_LOCALE[language]
    return DEFAULT_LOCALE


def is_language_chosen(language: str | None) -> bool:
    return language in _CODE_TO_LOCALE


_CODE_TO_LOCALE = {locale.value: locale for locale in Locale}
