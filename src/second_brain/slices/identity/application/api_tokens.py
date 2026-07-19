"""Жизненный цикл токенов доступа к публичному HTTP-API (эпик API-1, секция C).

Здесь живут четыре операции и ничего больше: выдать, перечислить, отозвать и
проверить предъявленный секрет. Сам роутер `/v1` и проверка токена на входе HTTP
— следующий слайс; этот даёт ему готовый механизм.

Секрет существует ровно один раз — в ответе на выдачу. В базу уходит только его
хэш с ОТДЕЛЬНЫМ перцем (не тем, которым солятся приглашения): ротация перца
инвайтов не должна разлогинивать все выданные API-токены.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hmac import digest
from secrets import token_urlsafe
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from second_brain.shared.clock import Clock
from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.ports.repositories import (
    ApiTokenPrincipal,
    ApiTokenRepository,
    ApiTokenTransaction,
    ApiTokenView,
    NewApiToken,
)

# Метка по умолчанию: сквозной номер токена у владельца. Кнопка в боте текст не
# спрашивает, а операция принимает произвольную метку — HTTP-клиент сможет
# задать свою.
DEFAULT_LABEL_TEMPLATE = "api-{number}"
# Длина секрета в байтах энтропии (как у ссылки-приглашения).
TOKEN_ENTROPY_BYTES = 32


@dataclass(frozen=True)
class IssuedApiToken:
    """Только что выданный токен: строка списка + секрет, который виден ОДИН раз.

    ``repr=False`` на секрете обязателен: объект не должен уметь напечатать
    плейнтекст ни в логе, ни в трейсбеке.
    """

    view: ApiTokenView
    secret: str = field(repr=False)


class ApiTokenLifecycle:
    """Выдача, список и отзыв токенов владельца внутри его же транзакции."""

    def __init__(self, pepper: bytes, pepper_key_id: str) -> None:
        self._pepper = pepper
        self._pepper_key_id = pepper_key_id

    async def issue(
        self,
        access_context: AccessContext,
        transaction: ApiTokenTransaction,
        now: datetime,
        label: str | None = None,
    ) -> IssuedApiToken:
        secret = token_urlsafe(TOKEN_ENTROPY_BYTES)
        if label is None:
            existing = await transaction.list_api_tokens(access_context)
            label = DEFAULT_LABEL_TEMPLATE.format(number=len(existing) + 1)
        view = await transaction.issue_api_token(
            access_context,
            NewApiToken(
                id=uuid4(),
                token_hash=self._hash(secret),
                pepper_key_id=self._pepper_key_id,
                label=label,
                created_at=now,
            ),
        )
        return IssuedApiToken(view=view, secret=secret)

    async def list_tokens(
        self, access_context: AccessContext, transaction: ApiTokenTransaction
    ) -> tuple[ApiTokenView, ...]:
        return await transaction.list_api_tokens(access_context)

    async def list_tokens_in_space_timezone(
        self, access_context: AccessContext, transaction: ApiTokenTransaction
    ) -> tuple[ApiTokenView, ...]:
        """Тот же список, но с моментами в часовом поясе пространства.

        Пользователю показываются его даты, а не UTC — как в подтверждении
        напоминания и в сводке.
        """
        views = await self.list_tokens(access_context, transaction)
        timezone = await transaction.read_user_space_timezone(access_context)
        if timezone is None:
            return views
        zone = ZoneInfo(timezone)
        return tuple(
            ApiTokenView(
                id=view.id,
                label=view.label,
                created_at=view.created_at.astimezone(zone),
                last_used_at=_in_zone(view.last_used_at, zone),
                revoked_at=_in_zone(view.revoked_at, zone),
            )
            for view in views
        )

    async def revoke(
        self,
        access_context: AccessContext,
        transaction: ApiTokenTransaction,
        token_id: UUID,
        now: datetime,
    ) -> bool:
        """Отзыв по идентификатору; повторный отзыв — не ошибка (True, без правки).

        False означает ровно одно: такого токена у ЭТОГО владельца нет.
        """
        return await transaction.revoke_api_token(access_context, token_id, now)

    def _hash(self, secret: str) -> bytes:
        return digest(self._pepper, secret.encode(), "sha256")


class AuthenticateApiToken:
    """Проверка предъявленного секрета: кому он принадлежит и жив ли он.

    Отметка «последний раз использован» пишется НЕ на каждый запрос: иначе
    каждое чтение через API превращалось бы в запись в базу. Отметка
    обновляется не чаще раза в ``last_used_throttle`` (параметр конфигурируемый,
    ``API_TOKEN_LAST_USED_THROTTLE_SECONDS``) — для ответа на вопрос «когда
    токеном пользовались в последний раз» такой точности достаточно.
    """

    def __init__(
        self,
        repository: ApiTokenRepository,
        clock: Clock,
        pepper: bytes,
        pepper_key_id: str,
        last_used_throttle: timedelta,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._pepper = pepper
        self._pepper_key_id = pepper_key_id
        self._last_used_throttle = last_used_throttle

    async def execute(self, secret: str) -> ApiTokenPrincipal | None:
        now = self._clock.now()
        return await self._repository.authenticate_api_token(
            token_hash=digest(self._pepper, secret.encode(), "sha256"),
            pepper_key_id=self._pepper_key_id,
            now=now,
            refresh_used_before=now - self._last_used_throttle,
        )


def _in_zone(moment: datetime | None, zone: ZoneInfo) -> datetime | None:
    return None if moment is None else moment.astimezone(zone)
