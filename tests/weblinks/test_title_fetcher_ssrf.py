"""SSRF-контракт HTTP-фетчера title (спека §1.2, ADR-0010) — юниты без сети.

Фейковые резолвер и транспорт: проверяем, что фетчер отклоняет приватные
адреса ДО похода в сеть, перепроверяет каждый redirect-hop, ограничивает
редиректы/размер/тип содержимого и мягко фейлится, а не кидает.
"""

import gzip
from collections.abc import Sequence

import pytest

from second_brain.slices.weblinks.adapters.http.title_fetcher import (
    FetchResponse,
    FetchTarget,
    UrlTitleFetcher,
)

MAX_BYTES = 4096
MAX_TITLE_LENGTH = 60


class FakeTransport:
    """Скриптованный транспорт: очередь ответов + журнал (target, ip)."""

    def __init__(self, responses: list[FetchResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[FetchTarget, str]] = []

    def __call__(
        self, target: FetchTarget, ip: str, timeout_seconds: float, max_bytes: int
    ) -> FetchResponse:
        self.calls.append((target, ip))
        if not self._responses:
            raise AssertionError("transport called more times than scripted")
        response = self._responses.pop(0)
        return FetchResponse(
            status=response.status,
            headers=response.headers,
            body=response.body[: max_bytes + 1],
        )


def resolver_map(mapping: dict[str, Sequence[str]]):
    def resolve(host: str) -> Sequence[str]:
        return mapping[host]

    return resolve


def html_response(
    body: bytes,
    content_type: str = "text/html; charset=utf-8",
    status: int = 200,
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> FetchResponse:
    return FetchResponse(
        status=status,
        headers=(("Content-Type", content_type), *extra_headers),
        body=body,
    )


def redirect_response(location: str) -> FetchResponse:
    return FetchResponse(status=302, headers=(("Location", location),), body=b"")


def fetcher(
    transport: FakeTransport,
    resolver,
    max_redirects: int = 3,
) -> UrlTitleFetcher:
    return UrlTitleFetcher(
        timeout_seconds=5,
        max_bytes=MAX_BYTES,
        max_redirects=max_redirects,
        max_title_length=MAX_TITLE_LENGTH,
        resolver=resolver,
        transport=transport,
    )


@pytest.mark.asyncio
async def test_happy_path_extracts_unescaped_collapsed_title() -> None:
    transport = FakeTransport(
        [
            html_response(
                b"<html><head><title>\n  Big &amp; small\n\tnews  </title></head>"
                b"<body>x</body></html>"
            )
        ]
    )
    outcome = await fetcher(
        transport, resolver_map({"example.com": ["93.184.216.34"]})
    ).fetch_title("https://example.com/story")

    assert outcome.ok
    assert outcome.title == "Big & small news"
    (target, ip) = transport.calls[0]
    assert (target.scheme, target.host, target.port) == ("https", "example.com", 443)
    assert ip == "93.184.216.34"


@pytest.mark.asyncio
async def test_title_is_truncated_to_the_configured_length() -> None:
    long_title = "т" * (MAX_TITLE_LENGTH * 2)
    transport = FakeTransport([html_response(f"<title>{long_title}</title>".encode())])
    outcome = await fetcher(
        transport, resolver_map({"example.com": ["93.184.216.34"]})
    ).fetch_title("https://example.com/")

    assert outcome.ok
    assert outcome.title == "т" * MAX_TITLE_LENGTH


@pytest.mark.asyncio
async def test_charset_from_the_content_type_header_is_respected() -> None:
    body = "<title>Привет</title>".encode("cp1251")
    transport = FakeTransport(
        [html_response(body, content_type="text/html; charset=windows-1251")]
    )
    outcome = await fetcher(
        transport, resolver_map({"example.com": ["93.184.216.34"]})
    ).fetch_title("http://example.com/")

    assert outcome.ok
    assert outcome.title == "Привет"


@pytest.mark.asyncio
async def test_page_without_title_is_fetched_softly_with_none() -> None:
    transport = FakeTransport([html_response(b"<html><body>no title</body></html>")])
    outcome = await fetcher(
        transport, resolver_map({"example.com": ["93.184.216.34"]})
    ).fetch_title("http://example.com/")

    assert outcome.ok
    assert outcome.title is None


@pytest.mark.asyncio
async def test_private_ip_is_rejected_before_any_transport_call() -> None:
    transport = FakeTransport([])
    outcome = await fetcher(
        transport, resolver_map({"internal.example": ["10.0.0.5"]})
    ).fetch_title("http://internal.example/admin")

    assert not outcome.ok
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "169.254.1.1", "224.0.0.1", "240.0.0.1", "0.0.0.0", "::1", "fe80::1"],
)
async def test_loopback_linklocal_multicast_reserved_are_rejected(
    address: str,
) -> None:
    transport = FakeTransport([])
    outcome = await fetcher(
        transport, resolver_map({"evil.example": [address]})
    ).fetch_title("http://evil.example/")

    assert not outcome.ok
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["100.64.0.1", "::ffff:127.0.0.1"])
async def test_cgnat_and_mapped_loopback_are_rejected(address: str) -> None:
    # Гейт — is_global: перечисление флагов пропускало CGNAT 100.64.0.0/10
    # («ни приватный, ни публичный») и IPv4-mapped loopback.
    transport = FakeTransport([])
    outcome = await fetcher(
        transport, resolver_map({"evil.example": [address]})
    ).fetch_title("http://evil.example/")

    assert not outcome.ok
    assert transport.calls == []


@pytest.mark.asyncio
async def test_control_and_bidi_characters_are_stripped_from_the_title() -> None:
    # NUL уронил бы INSERT в PostgreSQL (вечный ретрай), RTL-override
    # (‮) спуфил бы строку «title — url» в выдаче.
    transport = FakeTransport([html_response("<title>A\x00B ‮good</title>".encode())])
    outcome = await fetcher(
        transport, resolver_map({"example.com": ["93.184.216.34"]})
    ).fetch_title("http://example.com/")

    assert outcome.ok
    assert outcome.title == "AB good"


@pytest.mark.asyncio
async def test_zwj_emoji_in_the_title_stays_intact() -> None:
    # ZWJ (‍, категория Cf) легитимен: семья 👨‍👩‍👧 не должна распадаться
    # на отдельные эмодзи — режем только Cc и явные bidi-управляющие.
    family = "\U0001f468‍\U0001f469‍\U0001f467"
    transport = FakeTransport(
        [html_response(f"<title>Наша {family} страница</title>".encode())]
    )
    outcome = await fetcher(
        transport, resolver_map({"example.com": ["93.184.216.34"]})
    ).fetch_title("http://example.com/")

    assert outcome.ok
    assert outcome.title == f"Наша {family} страница"


@pytest.mark.asyncio
async def test_a_single_private_ip_among_public_ones_rejects_the_host() -> None:
    # Каждый резолвнутый адрес проверяется: DNS с одним «хорошим» и одним
    # приватным A-record — отказ (иначе rebinding-лотерея).
    transport = FakeTransport([])
    outcome = await fetcher(
        transport, resolver_map({"mixed.example": ["93.184.216.34", "192.168.1.1"]})
    ).fetch_title("http://mixed.example/")

    assert not outcome.ok
    assert transport.calls == []


@pytest.mark.asyncio
async def test_redirect_hop_to_a_private_host_is_rejected() -> None:
    transport = FakeTransport([redirect_response("http://internal.example/steal")])
    outcome = await fetcher(
        transport,
        resolver_map(
            {"public.example": ["93.184.216.34"], "internal.example": ["10.1.2.3"]}
        ),
    ).fetch_title("http://public.example/")

    assert not outcome.ok
    # Первый hop сходил, второй отвергнут ДО транспорта.
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_redirect_ceiling_fails_softly() -> None:
    transport = FakeTransport([redirect_response("http://public.example/next")] * 3)
    outcome = await fetcher(
        transport,
        resolver_map({"public.example": ["93.184.216.34"]}),
        max_redirects=2,
    ).fetch_title("http://public.example/")

    assert not outcome.ok
    # max_redirects=2 → максимум 3 похода (исходный + 2 hop'а), дальше стоп.
    assert len(transport.calls) == 3


@pytest.mark.asyncio
async def test_relative_redirect_is_followed_against_the_current_url() -> None:
    transport = FakeTransport(
        [redirect_response("/moved"), html_response(b"<title>Moved</title>")]
    )
    outcome = await fetcher(
        transport, resolver_map({"public.example": ["93.184.216.34"]})
    ).fetch_title("http://public.example/old")

    assert outcome.ok
    assert outcome.title == "Moved"
    assert transport.calls[1][0].path == "/moved"


@pytest.mark.asyncio
async def test_userinfo_in_the_url_is_rejected_without_resolving() -> None:
    def exploding_resolver(host: str) -> Sequence[str]:
        raise AssertionError("resolver must not be called for userinfo URLs")

    transport = FakeTransport([])
    outcome = await fetcher(transport, exploding_resolver).fetch_title(
        "http://user:pass@example.com/"
    )

    assert not outcome.ok
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "мусор"])
async def test_non_http_schemes_are_rejected(url: str) -> None:
    transport = FakeTransport([])
    outcome = await fetcher(
        transport, resolver_map({"example.com": ["93.184.216.34"]})
    ).fetch_title(url)

    assert not outcome.ok
    assert transport.calls == []


@pytest.mark.asyncio
async def test_non_html_content_type_fails_softly() -> None:
    transport = FakeTransport(
        [html_response(b'{"a": 1}', content_type="application/json")]
    )
    outcome = await fetcher(
        transport, resolver_map({"example.com": ["93.184.216.34"]})
    ).fetch_title("http://example.com/api")

    assert not outcome.ok


@pytest.mark.asyncio
async def test_oversized_body_fails_softly() -> None:
    transport = FakeTransport(
        [html_response(b"<title>big</title>" + b"x" * (MAX_BYTES * 2))]
    )
    outcome = await fetcher(
        transport, resolver_map({"example.com": ["93.184.216.34"]})
    ).fetch_title("http://example.com/huge")

    assert not outcome.ok


@pytest.mark.asyncio
async def test_gzip_bomb_is_capped_on_the_decompressed_size() -> None:
    # Маленькое сжатое тело, распаковывающееся сильно за cap: отказ.
    bomb = gzip.compress(b"<title>boom</title>" + b"a" * (MAX_BYTES * 50))
    assert len(bomb) <= MAX_BYTES
    transport = FakeTransport(
        [html_response(bomb, extra_headers=(("Content-Encoding", "gzip"),))]
    )
    outcome = await fetcher(
        transport, resolver_map({"example.com": ["93.184.216.34"]})
    ).fetch_title("http://example.com/bomb")

    assert not outcome.ok


@pytest.mark.asyncio
async def test_small_gzip_body_is_decompressed_and_parsed() -> None:
    body = gzip.compress("<title>Сжатый заголовок</title>".encode())
    transport = FakeTransport(
        [html_response(body, extra_headers=(("Content-Encoding", "gzip"),))]
    )
    outcome = await fetcher(
        transport, resolver_map({"example.com": ["93.184.216.34"]})
    ).fetch_title("http://example.com/gz")

    assert outcome.ok
    assert outcome.title == "Сжатый заголовок"


@pytest.mark.asyncio
async def test_transport_exception_becomes_a_soft_failure() -> None:
    def broken_transport(
        target: FetchTarget, ip: str, timeout_seconds: float, max_bytes: int
    ) -> FetchResponse:
        raise OSError("connection reset")

    outcome = await UrlTitleFetcher(
        timeout_seconds=5,
        max_bytes=MAX_BYTES,
        max_redirects=3,
        max_title_length=MAX_TITLE_LENGTH,
        resolver=resolver_map({"example.com": ["93.184.216.34"]}),
        transport=broken_transport,
    ).fetch_title("http://example.com/")

    assert not outcome.ok
