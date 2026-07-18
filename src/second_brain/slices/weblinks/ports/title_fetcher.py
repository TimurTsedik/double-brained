"""Порт HTTP-фетчера <title>: воркер зависит от контракта, не от транспорта."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class TitleFetchOutcome:
    """Итог одного фетча.

    ``ok=True`` — страница честно прочитана (``title`` может быть None: у
    страницы нет <title>, ретраить нечего). ``ok=False`` — мягкий сбой
    (сеть/редиректы/SSRF-отказ/не-HTML): строка остаётся pending до потолка
    попыток. Фетчер НИКОГДА не кидает — любой сбой сворачивается в ok=False.
    """

    ok: bool
    title: str | None = field(default=None, repr=False)


class TitleFetcher(Protocol):
    async def fetch_title(self, url: str) -> TitleFetchOutcome: ...
