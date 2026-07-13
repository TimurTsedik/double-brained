from pathlib import Path

import pytest

from tests.architecture.import_boundaries import find_violations, format_violations


def write_module(root: Path, relative_path: str, source: str) -> None:
    module_path = root / relative_path
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text(source, encoding="utf-8")


@pytest.mark.parametrize(
    ("relative_path", "source", "expected_module"),
    [
        ("slices/tasks/domain/task.py", "import fastapi\n", "fastapi"),
        (
            "slices/tasks/application/handler.py",
            "import sqlalchemy\n",
            "sqlalchemy",
        ),
        ("slices/tasks/domain/files.py", "import pathlib\n", "pathlib"),
        ("slices/tasks/domain/http_client.py", "import httpx\n", "httpx"),
        (
            "slices/tasks/adapters/worker/consumer.py",
            "import fastapi\n",
            "fastapi",
        ),
        (
            "slices/tasks/application/handler.py",
            "from second_brain.slices.knowledge.application.service import Service\n",
            "second_brain.slices.knowledge.application.service",
        ),
        (
            "slices/tasks/domain/task.py",
            "from second_brain.slices.tasks.domain_models import Task\n",
            "second_brain.slices.tasks.domain_models",
        ),
        (
            "slices/tasks/application/handler.py",
            "from second_brain.slices.tasks.infrastructure.mailer import Mailer\n",
            "second_brain.slices.tasks.infrastructure.mailer",
        ),
        (
            "slices/tasks/domain/handler.py",
            "from ..application.command import Command\n",
            "second_brain.slices.tasks.application.command",
        ),
        (
            "shared/leak.py",
            (
                "from second_brain.slices.tasks.application.contracts "
                "import TaskContract\n"
            ),
            "second_brain.slices.tasks.application.contracts",
        ),
        (
            "shared/leak.py",
            "from second_brain.slices import tasks\n",
            "second_brain.slices",
        ),
        (
            "slices/tasks/domain/environment.py",
            "import os\n",
            "os",
        ),
        (
            "slices/tasks/application/worker.py",
            "import queue\n",
            "queue",
        ),
        (
            "other/feature/adapters/api/router.py",
            "import fastapi\n",
            "fastapi",
        ),
        (
            "slices/tasks/domain/file_input.py",
            "import fileinput\n",
            "fileinput",
        ),
        (
            "slices/identity/domain/user.py",
            "import aiogram\n",
            "aiogram",
        ),
        (
            "slices/identity/application/enroll.py",
            "import sqlalchemy\n",
            "sqlalchemy",
        ),
        (
            "slices/identity/adapters/persistence/repository.py",
            "import aiogram\n",
            "aiogram",
        ),
        (
            "slices/identity/adapters/telegram/poller.py",
            "import sqlalchemy\n",
            "sqlalchemy",
        ),
        (
            "slices/identity/adapters/telegram/poller.py",
            "import asyncpg\n",
            "asyncpg",
        ),
        (
            "slices/identity/adapters/persistence/schema.py",
            (
                "from second_brain.slices.capture.adapters.persistence.models "
                "import CaptureEventModel\n"
            ),
            "second_brain.slices.capture.adapters.persistence.models",
        ),
        (
            "slices/capture/adapters/persistence/models.py",
            (
                "from second_brain.slices.identity.adapters.persistence.models "
                "import Base\n"
            ),
            "second_brain.slices.identity.adapters.persistence.models",
        ),
    ],
)
def test_checker_reports_prohibited_import(
    tmp_path: Path,
    relative_path: str,
    source: str,
    expected_module: str,
) -> None:
    write_module(tmp_path, relative_path, source)

    violations = list(find_violations(tmp_path))

    assert [violation.imported_module for violation in violations] == [expected_module]
    assert relative_path in str(violations[0].path)
    assert expected_module in format_violations(violations)


def test_checker_allows_bootstrap_and_published_contract_imports(
    tmp_path: Path,
) -> None:
    write_module(tmp_path, "bootstrap/app.py", "from fastapi import FastAPI\n")
    write_module(
        tmp_path,
        "slices/tasks/adapters/api/router.py",
        "from fastapi import FastAPI\n",
    )
    write_module(
        tmp_path,
        "slices/identity/adapters/telegram/poller.py",
        "import aiogram\n",
    )
    write_module(
        tmp_path,
        "slices/identity/adapters/persistence/repository.py",
        "import sqlalchemy\nimport asyncpg\n",
    )
    write_module(tmp_path, "persistence/base.py", "import sqlalchemy\n")
    write_module(
        tmp_path,
        "slices/tasks/domain/handler.py",
        "from .model import Task\n",
    )
    write_module(
        tmp_path,
        "slices/tasks/application/handler.py",
        (
            "from second_brain.shared.clock import Clock\n"
            "from second_brain.slices.knowledge.application.contracts "
            "import KnowledgeContract\n"
            "from second_brain.slices.tasks.application.command import Command\n"
            "from second_brain.slices.tasks.domain.task import Task\n"
            "from second_brain.slices.tasks.ports.clock import TaskClock\n"
        ),
    )

    violations = list(find_violations(tmp_path))

    assert not violations, format_violations(violations)


def test_real_package_obeys_import_boundaries() -> None:
    package_root = Path(__file__).parents[2] / "src" / "second_brain"
    violations = list(find_violations(package_root))

    assert not violations, format_violations(violations)


def test_capture_transaction_composition_is_limited_to_bootstrap() -> None:
    package_root = Path(__file__).parents[2] / "src" / "second_brain"
    modules_using_both_transaction_and_capture_writer = []

    for path in package_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if (
            "PostgresUpdateTransaction" in source
            and "PostgresCaptureEventWriter" in source
        ):
            modules_using_both_transaction_and_capture_writer.append(
                path.relative_to(package_root)
            )

    assert modules_using_both_transaction_and_capture_writer == [
        Path("bootstrap/capture_in_transaction.py"),
        Path("bootstrap/task_capture_in_transaction.py"),
    ]


def test_task_capture_transaction_composition_is_limited_to_bootstrap() -> None:
    package_root = Path(__file__).parents[2] / "src" / "second_brain"
    violating_paths: list[Path] = []
    forbidden_imports = (
        "second_brain.slices.identity.adapters.persistence",
        "second_brain.slices.capture.adapters.persistence",
    )

    for path in package_root.joinpath("slices", "tasks").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if any(imported in source for imported in forbidden_imports):
            violating_paths.append(path.relative_to(package_root))

    assert violating_paths == []
    composition = package_root / "bootstrap" / "task_capture_in_transaction.py"
    composition_source = composition.read_text(encoding="utf-8")
    assert "PostgresUpdateTransaction" in composition_source
    assert "PostgresCaptureEventWriter" in composition_source
    assert "PostgresPendingCaptureSelectionWriter" in composition_source
