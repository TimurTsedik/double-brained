"""Нормализация URL для дедупликации page_titles (спека §1.2).

scheme/host — lowercase, хост — IDNA, fragment отбрасывается, дефолтные порты
(:80/:443) убираются; query и path сохраняются как есть.
"""

import pytest

from second_brain.slices.weblinks.adapters.normalization import normalize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # scheme/host — lower; path и query — как есть; fragment — прочь.
        (
            "HTTP://ExAmPle.COM/PaTh?Q=1&b=2#frag",
            "http://example.com/PaTh?Q=1&b=2",
        ),
        # Дефолтные порты убираются, недефолтные остаются.
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/", "http://example.com/"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        # IDNA-хост.
        ("https://Пример.РФ/путь", "https://xn--e1afmkfd.xn--p1ai/путь"),
        # Пустой путь и query сохраняются буквально.
        ("https://example.com", "https://example.com"),
        ("https://example.com/?q=%20x", "https://example.com/?q=%20x"),
    ],
)
def test_normalize_url_canonical_forms(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_url_is_idempotent() -> None:
    once = normalize_url("HTTPS://Пример.РФ:443/путь?q=1#x")
    assert once is not None
    assert normalize_url(once) == once


@pytest.mark.parametrize(
    "raw",
    [
        # Неканонизируемое → None: такой URL НЕ ставится в очередь титулов,
        # иначе мусорный вариант занял бы слот дедупа (user_space_id,
        # normalized_url) и навсегда заблокировал бы титул нормальной формы.
        "http://user:pass@example.com/a",
        "http://example.com:99999/a",
        "ftp://example.com/a",
        "file:///etc/passwd",
        "мусор без схемы",
        "http:///path-without-host",
        # Синтаксически битый URL (незакрытая скобка IPv6): urlsplit кидает
        # ValueError — normalize_url обязан вернуть None, не уронить захват.
        "http://[::1",
    ],
)
def test_non_canonicalizable_urls_yield_none(raw: str) -> None:
    assert normalize_url(raw) is None
