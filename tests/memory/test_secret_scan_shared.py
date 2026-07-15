import pytest

from second_brain.shared.secret_scan import contains_credential


@pytest.mark.parametrize(
    "secret",
    [
        "-----BEGIN PRIVATE KEY-----\nprivate-material",
        "api_key = sk-examplevalue12345678901234567890",
        "https://github.com push ghp_abcdefghijklmnopqrstuvwxyz0123",
        "creds AKIAABCDEFGHIJKLMNOP end",
        "token: 123456789:abcdefghijklmnopqrstuvwxyzABCDE12345",
        "password=hunter2",
    ],
)
def test_credential_patterns_are_detected(secret: str) -> None:
    assert contains_credential(secret) is True


@pytest.mark.parametrize(
    "clean",
    [
        "Надо обсудить token budget модели",
        "Обычная заметка про пароли и токены без значений",
        "Зачем мы искали ФИО пассажира через номер БСО?",
    ],
)
def test_clean_text_passes(clean: str) -> None:
    assert contains_credential(clean) is False


def test_classification_reuses_the_shared_scanner() -> None:
    from second_brain.shared import secret_scan
    from second_brain.slices.classification.application import extraction

    assert extraction.contains_credential is secret_scan.contains_credential
