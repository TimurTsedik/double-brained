from datetime import UTC, datetime
from uuid import uuid4

from second_brain.slices.contacts.application.matching import append_matched_phones
from second_brain.slices.contacts.domain.entities import Contact

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def contact(name: str, phone: str) -> Contact:
    return Contact(
        id=uuid4(),
        user_space_id=uuid4(),
        display_name=name,
        phone_number=phone,
        created_at=NOW,
        updated_at=NOW,
        trace_id="1" * 32,
    )


def test_known_name_appends_the_phone() -> None:
    result = append_matched_phones(
        "позвонить Ави завтра", [contact("Ави", "+972-50-111-22-33")]
    )

    assert result == "позвонить Ави завтра · +972-50-111-22-33"


def test_name_at_the_end_of_text_matches() -> None:
    result = append_matched_phones(
        "позвонить Ави", [contact("Ави", "+972-50-111-22-33")]
    )

    assert result == "позвонить Ави · +972-50-111-22-33"


def test_matching_is_case_insensitive_with_unicode_casefold() -> None:
    result = append_matched_phones(
        "позвонить АВИ срочно", [contact("ави", "+972-50-111-22-33")]
    )

    assert result == "позвонить АВИ срочно · +972-50-111-22-33"


def test_name_inside_another_word_does_not_match() -> None:
    # «Ави» входит в «правил» и «доставить» как подстрока — границы слова
    # обязаны отсечь оба ложных срабатывания.
    contacts = [contact("Ави", "+972-50-111-22-33")]

    assert append_matched_phones("выучить правила", contacts) == "выучить правила"
    assert append_matched_phones("доставить посылку", contacts) == "доставить посылку"


def test_name_delimited_by_punctuation_matches() -> None:
    result = append_matched_phones(
        "позвонить Ави!", [contact("Ави", "+972-50-111-22-33")]
    )

    assert result == "позвонить Ави! · +972-50-111-22-33"


def test_two_known_names_append_both_phones() -> None:
    result = append_matched_phones(
        "позвонить Ави и Маше",
        [
            contact("Ави", "+972-50-111-22-33"),
            contact("Маше", "+972-50-444-55-66"),
        ],
    )

    assert result == "позвонить Ави и Маше · +972-50-111-22-33 · +972-50-444-55-66"


def test_no_match_leaves_the_text_unchanged() -> None:
    result = append_matched_phones("купить хлеб", [contact("Ави", "+972-50-111-22-33")])

    assert result == "купить хлеб"


def test_no_contacts_leave_the_text_unchanged() -> None:
    assert append_matched_phones("позвонить Ави", []) == "позвонить Ави"


def test_contact_entity_hides_pii_from_repr() -> None:
    entity = contact("Ави Секрет", "+972-50-111-22-33")

    assert "Ави" not in repr(entity)
    assert "+972-50-111-22-33" not in repr(entity)
