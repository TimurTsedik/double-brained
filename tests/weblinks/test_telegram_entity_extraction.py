"""Извлечение ссылок из Telegram-entities при нормализации апдейта.

Offsets/length у Telegram — UTF-16 code units, НЕ индексы Python-строки:
эмодзи вне BMP занимает ДВА юнита. Маппер обязан переводить честно, иначе
label уезжает на символ при каждом эмодзи перед ссылкой.
"""

from types import SimpleNamespace
from typing import cast

from aiogram import Bot
from aiogram.types import Update

from second_brain.slices.identity.adapters.telegram.gateway import AiogramGateway
from tests.identity.locale_fakes import FakeLocaleResolver


def utf16_units(value: str) -> int:
    """Длина строки в UTF-16 code units — так же считает Telegram."""
    return len(value.encode("utf-16-le")) // 2


def gateway() -> AiogramGateway:
    return AiogramGateway(
        cast(Bot, object()), bot_id=1, locale_resolver=FakeLocaleResolver()
    )


def entity(
    entity_type: str, text: str, fragment: str, url: str | None = None
) -> SimpleNamespace:
    """Entity над первым вхождением fragment, offsets — в UTF-16 юнитах."""
    prefix = text[: text.index(fragment)]
    return SimpleNamespace(
        type=entity_type,
        offset=utf16_units(prefix),
        length=utf16_units(fragment),
        url=url,
    )


def normalize(text: str, entities: list[SimpleNamespace]):
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(type="private"),
        text=text,
        message_id=200,
        voice=None,
        contact=None,
        entities=entities,
    )
    update = SimpleNamespace(update_id=100, callback_query=None, message=message)
    return gateway()._normalize(cast(Update, update))


def test_text_link_label_is_the_covered_substring() -> None:
    text = "смотри доклад тут и не забудь"
    normalized = normalize(
        text, [entity("text_link", text, "тут", url="https://example.com/talk")]
    )

    assert [(link.label, link.url) for link in normalized.links] == [
        ("тут", "https://example.com/talk")
    ]
    assert normalized.text == text


def test_bare_url_label_equals_the_url_itself() -> None:
    text = "см. https://example.com/x?a=1"
    normalized = normalize(text, [entity("url", text, "https://example.com/x?a=1")])

    assert [(link.label, link.url) for link in normalized.links] == [
        ("https://example.com/x?a=1", "https://example.com/x?a=1")
    ]


def test_emoji_before_the_link_does_not_shift_the_label() -> None:
    # 🔥 вне BMP: в UTF-16 это 2 юнита, в Python-строке — 1 символ. Без
    # честного маппера label уехал бы на два символа влево.
    text = "🔥🔥 горящий доклад тут же"
    normalized = normalize(
        text, [entity("text_link", text, "тут", url="https://example.com/hot")]
    )

    assert [(link.label, link.url) for link in normalized.links] == [
        ("тут", "https://example.com/hot")
    ]


def test_emoji_inside_the_label_survives_the_mapping() -> None:
    text = "запись 🚀 запуска и итоги"
    normalized = normalize(
        text,
        [entity("text_link", text, "🚀 запуска", url="https://example.com/launch")],
    )

    assert [(link.label, link.url) for link in normalized.links] == [
        ("🚀 запуска", "https://example.com/launch")
    ]


def test_emoji_before_a_bare_url_keeps_the_url_intact() -> None:
    text = "🎯 цель: https://example.com/goal — прочесть"
    normalized = normalize(text, [entity("url", text, "https://example.com/goal")])

    assert [(link.label, link.url) for link in normalized.links] == [
        ("https://example.com/goal", "https://example.com/goal")
    ]


def test_multiple_links_keep_their_order() -> None:
    text = "🧠 раз https://a.example и два здесь конец"
    entities = [
        entity("url", text, "https://a.example"),
        entity("text_link", text, "здесь", url="https://b.example/2"),
    ]
    normalized = normalize(text, entities)

    assert [(link.label, link.url) for link in normalized.links] == [
        ("https://a.example", "https://a.example"),
        ("здесь", "https://b.example/2"),
    ]


def test_unrelated_entities_and_missing_entities_yield_no_links() -> None:
    text = "просто жирный текст"
    normalized = normalize(text, [entity("bold", text, "жирный")])
    assert normalized.links == ()

    # Сообщение вовсе без entities (старый SimpleNamespace без атрибута).
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(type="private"),
        text="без ссылок",
        message_id=201,
        voice=None,
        contact=None,
    )
    update = SimpleNamespace(update_id=101, callback_query=None, message=message)
    assert gateway()._normalize(cast(Update, update)).links == ()


def test_links_are_hidden_from_repr_like_other_pii_fields() -> None:
    text = "секретный доклад тут"
    normalized = normalize(
        text, [entity("text_link", text, "тут", url="https://secret.example/x")]
    )

    assert "secret.example" not in repr(normalized)
    assert "secret.example" not in repr(normalized.links[0])
