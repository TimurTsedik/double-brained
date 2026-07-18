"""Нормализация URL для дедупликации page_titles (спека §1.2).

Каноническая форма: scheme/host — lowercase, хост — IDNA (punycode),
fragment отбрасывается, дефолтные порты (:80/:443) убираются; query и path
сохраняются как есть. original_url хранится отдельно — как прислан.

``None`` — «не канонизируется»: не-http(s), userinfo (user:pass@), пустой
хост или немысленный порт. Такой URL НЕ ставится в очередь титулов — иначе
мусорный вариант занял бы слот дедупа (user_space_id, normalized_url) и
навсегда заблокировал бы титул для нормальной формы той же страницы.
"""

from urllib.parse import urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
_ALLOWED_SCHEMES = frozenset(_DEFAULT_PORTS)


def normalize_url(url: str) -> str | None:
    # ВСЁ чтение SplitResult под try: и сам urlsplit, и ленивые .username/
    # .hostname/.port кидают ValueError на битом синтаксисе («http://[::1»)
    # — без try такой URL уронил бы захват/показ целиком.
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        host = _normalize_host(parts.hostname or "")
        if not host:
            return None
        port = parts.port
    except ValueError:
        return None
    if port is not None and port == _DEFAULT_PORTS.get(scheme):
        port = None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def _normalize_host(host: str) -> str:
    # urlsplit().hostname уже lowercase; не-ASCII хост переводим в IDNA.
    # Хост, который IDNA не берёт (подчёркивания и пр.), остаётся lowercase.
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host
