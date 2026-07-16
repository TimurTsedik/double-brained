import re
from collections.abc import Sequence

from second_brain.slices.contacts.domain.entities import Contact


def append_matched_phones(text: str, contacts: Sequence[Contact]) -> str:
    """Дописать « · {номер}» за каждое имя контакта, упомянутое в тексте.

    Матчинг — Unicode casefold и ТОЛЬКО по границам слова: имя отделено
    началом/концом строки, пробелом или пунктуацией. «Ави» не срабатывает
    внутри «правил»/«доставить». Несколько имён → все номера по порядку
    списка контактов. Ничего не найдено → текст как был.
    """
    for contact in contacts:
        if _mentions(text, contact.display_name):
            text = f"{text} · {contact.phone_number}"
    return text


def _mentions(text: str, name: str) -> bool:
    folded_name = name.casefold()
    if not folded_name:
        return False
    # (?<!\w)/(?!\w): по обе стороны имени НЕ буква/цифра/подчёркивание —
    # то есть границы строки, пробелы или пунктуация.
    pattern = rf"(?<!\w){re.escape(folded_name)}(?!\w)"
    return re.search(pattern, text.casefold()) is not None
