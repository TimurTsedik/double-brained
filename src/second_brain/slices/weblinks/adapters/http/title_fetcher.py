"""Ограниченный HTTP-фетчер <title> с SSRF-контрактом (спека §1.2, ADR-0010).

Правила, каждое — обязательное:
- только http/https; userinfo (user:pass@) — отказ до резолва;
- хост резолвится, и КАЖДЫЙ адрес проверяется: private/loopback/link-local/
  multicast/reserved/unspecified → отказ; коннект — строго на проверенный
  публичный IP (защита от DNS-rebinding);
- редиректы ТОЛЬКО вручную, каждый hop проходит все проверки заново,
  потолок — max_redirects;
- cap max_bytes и на сжатое, и на распакованное тело; Content-Type только
  text/html | application/xhtml+xml;
- <title> без полного парсера (regex), html.unescape, схлопнутые пробелы,
  срез до max_title_length; кодировка из заголовка/meta, fallback utf-8.

Любой сбой — мягкий: TitleFetchOutcome(ok=False), исключения наружу не летят.
"""

import asyncio
import html
import http.client
import ipaddress
import re
import socket
import ssl
import unicodedata
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from second_brain.slices.weblinks.ports.title_fetcher import TitleFetchOutcome

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_CHARSET_PATTERN = re.compile(rb"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)")
# Явные направляющие bidi (ALM, LRM/RLM, LRE..RLO, LRI..PDI): ими спуфится
# порядок символов в строке «title — url». Записаны escape'ами — невидимые
# литералы в исходнике нечитаемы и легко теряются при правках.
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


@dataclass(frozen=True)
class FetchTarget:
    """Проверенная цель одного hop'а: куда и что запрашивать."""

    scheme: str
    host: str
    port: int
    path: str


@dataclass(frozen=True)
class FetchResponse:
    """Сырой ответ транспорта; body прочитано максимум max_bytes+1."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None


Resolver = Callable[[str], Sequence[str]]
Transport = Callable[[FetchTarget, str, float, int], FetchResponse]


class _Rejected(Exception):
    """Внутренний сигнал мягкого отказа (SSRF/лимиты/не-HTML)."""


class UrlTitleFetcher:
    """TitleFetcher поверх stdlib: ручные редиректы, коннект на проверенный IP.

    ``resolver``/``transport`` инжектируются тестами (фейки без сети);
    по умолчанию — системный DNS и http.client с TLS-SNI на исходный хост.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_bytes: int,
        max_redirects: int,
        max_title_length: int,
        resolver: Resolver | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._max_title_length = max_title_length
        self._resolver = resolver or _system_resolver
        self._transport = transport or _system_transport

    async def fetch_title(self, url: str) -> TitleFetchOutcome:
        # Синхронный stdlib-фетч уводится в поток: цикл воркера не блокируется.
        return await asyncio.to_thread(self._fetch_sync, url)

    def _fetch_sync(self, url: str) -> TitleFetchOutcome:
        try:
            response = self._follow_redirects(url)
            return self._parse_outcome(response)
        except Exception:
            # Любой сбой (SSRF-отказ, сеть, лимиты, кодировки) — мягкий.
            return TitleFetchOutcome(ok=False)

    def _follow_redirects(self, url: str) -> FetchResponse:
        current = url
        # max_redirects — потолок hop'ов ПОСЛЕ исходного запроса.
        for _hop in range(self._max_redirects + 1):
            target = self._validated_target(current)
            ip = self._checked_public_ip(target.host)
            response = self._transport(
                target, ip, self._timeout_seconds, self._max_bytes
            )
            if response.status in _REDIRECT_STATUSES:
                location = response.header("Location")
                if not location:
                    raise _Rejected("redirect without location")
                current = urljoin(current, location)
                continue
            if not (200 <= response.status < 300):
                raise _Rejected(f"status {response.status}")
            return response
        raise _Rejected("redirect ceiling exceeded")

    def _validated_target(self, url: str) -> FetchTarget:
        parts = urlsplit(url)
        if parts.scheme.lower() not in _ALLOWED_SCHEMES:
            raise _Rejected("scheme is not http/https")
        if parts.username is not None or parts.password is not None:
            raise _Rejected("userinfo in url")
        host = parts.hostname
        if not host:
            raise _Rejected("empty host")
        scheme = parts.scheme.lower()
        port = parts.port
        if port is None:
            port = _DEFAULT_PORTS[scheme]
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return FetchTarget(scheme=scheme, host=host, port=port, path=path)

    def _checked_public_ip(self, host: str) -> str:
        addresses = list(self._resolver(host))
        if not addresses:
            raise _Rejected("host did not resolve")
        # Проверяется КАЖДЫЙ адрес: один приватный A-record — отказ всему хосту
        # (иначе rebinding-лотерея). Гейт — `is_global`: перечисление флагов
        # (private/loopback/…) пропускало CGNAT 100.64.0.0/10, который «ни
        # приватный, ни публичный». Мультикаст отдельно: часть его глобальна
        # по скоупу, но целью фетча быть не может. Коннект — на первый адрес.
        for raw in addresses:
            address = ipaddress.ip_address(raw)
            if not address.is_global or address.is_multicast:
                raise _Rejected("resolved address is not public")
        return addresses[0]

    def _parse_outcome(self, response: FetchResponse) -> TitleFetchOutcome:
        content_type = response.header("Content-Type") or ""
        mime = content_type.partition(";")[0].strip().lower()
        if mime not in _ALLOWED_CONTENT_TYPES:
            raise _Rejected("content type is not html")
        body = response.body
        if len(body) > self._max_bytes:
            raise _Rejected("compressed body over cap")
        body = self._decoded_body(body, response.header("Content-Encoding"))
        page_text = self._decoded_text(body, content_type)
        match = _TITLE_PATTERN.search(page_text)
        if match is None:
            return TitleFetchOutcome(ok=True, title=None)
        title = " ".join(html.unescape(match.group(1)).split())
        # Вычистка до среза длины: Cc (NUL уронил бы INSERT в PostgreSQL —
        # вечный ретрай) + явные bidi-управляющие (RTL-спуфинг строки
        # «title — url»). Весь Cf НЕ режем: ZWJ/ZWNJ легитимны в составных
        # эмодзи и письменностях — семья 👨‍👩‍👧 не должна распадаться.
        title = "".join(
            ch
            for ch in title
            if unicodedata.category(ch) != "Cc" and ch not in _BIDI_CONTROLS
        )
        title = title[: self._max_title_length]
        return TitleFetchOutcome(ok=True, title=title or None)

    def _decoded_body(self, body: bytes, content_encoding: str | None) -> bytes:
        encoding = (content_encoding or "").strip().lower()
        if encoding in {"", "identity"}:
            return body
        if encoding not in {"gzip", "x-gzip", "deflate"}:
            raise _Rejected("unsupported content encoding")
        # Потоковый cap на РАСПАКОВАННОЕ тело: декомпрессор просят выдать не
        # больше max_bytes+1 — бомба ловится без раздувания памяти.
        decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 32)
        decompressed = decompressor.decompress(body, self._max_bytes + 1)
        if len(decompressed) > self._max_bytes:
            raise _Rejected("decompressed body over cap")
        return decompressed

    def _decoded_text(self, body: bytes, content_type: str) -> str:
        charset = _charset_from_header(content_type) or _charset_from_meta(body)
        for candidate in (charset, "utf-8"):
            if candidate is None:
                continue
            try:
                return body.decode(candidate, errors="replace")
            except LookupError:
                continue
        return body.decode("utf-8", errors="replace")


def _charset_from_header(content_type: str) -> str | None:
    for parameter in content_type.split(";")[1:]:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip("\"'") or None
    return None


def _charset_from_meta(body: bytes) -> str | None:
    match = _META_CHARSET_PATTERN.search(body)
    if match is None:
        return None
    return match.group(1).decode("ascii", errors="replace")


def _system_resolver(host: str) -> Sequence[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def _system_transport(
    target: FetchTarget, ip: str, timeout_seconds: float, max_bytes: int
) -> FetchResponse:
    # Коннект СТРОГО на проверенный IP; исходный хост живёт только в SNI и
    # заголовке Host — повторного (подменяемого) резолва не происходит.
    sock = socket.create_connection((ip, target.port), timeout=timeout_seconds)
    connection: http.client.HTTPConnection | None = None
    try:
        if target.scheme == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=target.host)
            connection = http.client.HTTPSConnection(
                target.host, target.port, timeout=timeout_seconds
            )
        else:
            connection = http.client.HTTPConnection(
                target.host, target.port, timeout=timeout_seconds
            )
        connection.sock = sock
        connection.request(
            "GET",
            target.path,
            headers={
                "Accept": "text/html, application/xhtml+xml",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        body = response.read(max_bytes + 1)
        return FetchResponse(
            status=response.status,
            headers=tuple(response.getheaders()),
            body=body,
        )
    finally:
        if connection is not None:
            connection.close()
        sock.close()
