from second_brain.shared.i18n import Locale
from second_brain.slices.identity.application.contracts import PanelContext


class FakeLocaleResolver:
    """Deterministic locale source for transport tests (no database)."""

    def __init__(self, locale: Locale = Locale.RU) -> None:
        self._locale = locale
        self.calls: list[int] = []

    async def resolve_for_telegram_user(self, telegram_user_id: int) -> Locale:
        self.calls.append(telegram_user_id)
        return self._locale


class FakePanelContextResolver:
    """Deterministic (locale, is_admin) source for panel tests (no database)."""

    def __init__(self, locale: Locale = Locale.RU, is_admin: bool = False) -> None:
        self._context = PanelContext(locale=locale, is_admin=is_admin)
        self.calls: list[int] = []

    async def resolve_panel_context(self, telegram_user_id: int) -> PanelContext:
        self.calls.append(telegram_user_id)
        return self._context
