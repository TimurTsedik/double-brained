from second_brain.shared.i18n import Locale


class FakeLocaleResolver:
    """Deterministic locale source for transport tests (no database)."""

    def __init__(self, locale: Locale = Locale.RU) -> None:
        self._locale = locale
        self.calls: list[int] = []

    async def resolve_for_telegram_user(self, telegram_user_id: int) -> Locale:
        self.calls.append(telegram_user_id)
        return self._locale
