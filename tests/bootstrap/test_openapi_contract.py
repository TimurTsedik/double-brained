"""Ворота контракта `/v1`: схема в репозитории обязана совпадать с приложением.

Опубликованная схема — обещание тому, у кого на руках токен. Пока она жила
только в памяти процесса, любая правка роутера меняла обещание молча: в дифф
попадал код, а не изменение контракта. Этот тест ставит файл
``contract/openapi.json`` на пост: разошлись — красный прогон, и в сообщении
сразу написана команда, которой правится расхождение.

База и сеть тесту не нужны: схема собирается из самого приложения, а зависимости
роутера собираются лениво, уже внутри запроса.
"""

import pytest

from second_brain.bootstrap.openapi_dump import CONTRACT_PATH, render_contract
from tests.bootstrap.conftest import set_required_environment


def test_the_committed_contract_matches_the_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_required_environment(monkeypatch)

    assert CONTRACT_PATH.read_text(encoding="utf-8") == render_contract(), (
        "Схема `/v1` разошлась с файлом contract/openapi.json. "
        "Перегенерируйте артефакт и закоммитьте его: uv run second-brain-openapi"
    )
