import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from sys import stdlib_module_names


@dataclass(frozen=True)
class Violation:
    path: Path
    imported_module: str
    rule: str


def find_violations(package_root: Path) -> Iterable[Violation]:
    for path in sorted(package_root.rglob("*.py")):
        module_path = path.relative_to(package_root).with_suffix("").parts
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        for imported_module in imported_modules(tree, module_path):
            rule = violation_rule(module_path, imported_module)
            if rule is not None:
                yield Violation(
                    path=path,
                    imported_module=imported_module,
                    rule=rule,
                )


def format_violations(violations: Iterable[Violation]) -> str:
    return "\n".join(
        f"{violation.path}: {violation.imported_module}: {violation.rule}"
        for violation in violations
    )


def imported_modules(tree: ast.Module, module_path: tuple[str, ...]) -> Iterable[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            yield resolve_import_from(node, module_path)


def resolve_import_from(node: ast.ImportFrom, module_path: tuple[str, ...]) -> str:
    if node.level == 0:
        return node.module or ""

    package_path = module_path[:-1]
    parent_path = package_path[: len(package_path) - (node.level - 1)]
    parts = ("second_brain", *parent_path)
    if node.module is not None:
        parts += tuple(node.module.split("."))
    return ".".join(parts)


def violation_rule(module_path: tuple[str, ...], imported_module: str) -> str | None:
    if is_shared(module_path) and is_module_or_descendant(
        imported_module, "second_brain.slices"
    ):
        return "shared must not import a business slice"

    if identity_persistence_imports_capture_persistence(module_path, imported_module):
        return "identity persistence must not import capture persistence"

    if capture_persistence_imports_identity_persistence(module_path, imported_module):
        return "capture persistence must not import identity persistence"

    if imports_internal_module_of_another_slice(module_path, imported_module):
        return "cross-slice imports must use published application contracts"

    if imports_aiogram(imported_module) and not aiogram_import_allowed(module_path):
        return "aiogram is allowed only in bootstrap or adapters/telegram"

    if imports_embedding_runtime(imported_module) and not embedding_import_allowed(
        module_path
    ):
        return (
            "sentence-transformers, transformers and torch are allowed only in "
            "bootstrap or adapters/embedding"
        )

    if imports_persistence_library(imported_module) and not persistence_import_allowed(
        module_path
    ):
        return (
            "SQLAlchemy, asyncpg and pgvector are allowed only in bootstrap or "
            "adapters/persistence"
        )

    if is_domain(module_path):
        if not domain_import_allowed(module_path, imported_module):
            return "domain import is not allowed"

    if is_application(module_path):
        if not application_import_allowed(module_path, imported_module):
            return "application import is not allowed"

    if imports_fastapi(imported_module) and not fastapi_import_allowed(module_path):
        return "FastAPI is allowed only in bootstrap or adapters/api"

    return None


def imports_internal_module_of_another_slice(
    module_path: tuple[str, ...], imported_module: str
) -> bool:
    if len(module_path) < 2 or module_path[0] != "slices":
        return False
    imported_parts = imported_module.split(".")
    if len(imported_parts) < 4 or imported_parts[:2] != ["second_brain", "slices"]:
        return False
    if imported_parts[2] == module_path[1] or is_published_contract(imported_module):
        return False
    return not is_documented_persistence_model_import(module_path, imported_module)


def is_documented_persistence_model_import(
    module_path: tuple[str, ...], imported_module: str
) -> bool:
    documented_imports: dict[tuple[str, ...], set[str]] = {
        (
            "slices",
            "retrieval",
            "adapters",
            "persistence",
            "repository",
        ): {
            "second_brain.slices.knowledge.adapters.persistence.models",
            "second_brain.slices.processing.adapters.persistence.models",
            "second_brain.slices.reminders.adapters.persistence.models",
            "second_brain.slices.tasks.adapters.persistence.models",
            "second_brain.slices.tasks.domain.entities",
        },
        (
            "slices",
            "projects",
            "adapters",
            "persistence",
            "repository",
        ): {
            "second_brain.slices.capture.adapters.persistence.models",
            "second_brain.slices.knowledge.adapters.persistence.models",
            "second_brain.slices.tasks.adapters.persistence.models",
        },
    }
    return imported_module in documented_imports.get(module_path, set())


def identity_persistence_imports_capture_persistence(
    module_path: tuple[str, ...], imported_module: str
) -> bool:
    return (
        len(module_path) >= 4
        and module_path[:4] == ("slices", "identity", "adapters", "persistence")
        and is_module_or_descendant(
            imported_module, "second_brain.slices.capture.adapters.persistence"
        )
    )


def capture_persistence_imports_identity_persistence(
    module_path: tuple[str, ...], imported_module: str
) -> bool:
    return (
        len(module_path) >= 4
        and module_path[:4] == ("slices", "capture", "adapters", "persistence")
        and is_module_or_descendant(
            imported_module, "second_brain.slices.identity.adapters.persistence"
        )
    )


def is_shared(module_path: tuple[str, ...]) -> bool:
    return module_path[:1] == ("shared",)


def is_domain(module_path: tuple[str, ...]) -> bool:
    return (
        len(module_path) >= 3
        and module_path[0] == "slices"
        and module_path[2] == "domain"
    )


def is_application(module_path: tuple[str, ...]) -> bool:
    return (
        len(module_path) >= 3
        and module_path[0] == "slices"
        and module_path[2] == "application"
    )


def imports_fastapi(imported_module: str) -> bool:
    return imported_module == "fastapi" or imported_module.startswith("fastapi.")


def imports_aiogram(imported_module: str) -> bool:
    return imported_module == "aiogram" or imported_module.startswith("aiogram.")


def imports_persistence_library(imported_module: str) -> bool:
    return imported_module.partition(".")[0] in {"asyncpg", "pgvector", "sqlalchemy"}


def imports_embedding_runtime(imported_module: str) -> bool:
    return imported_module.partition(".")[0] in {
        "sentence_transformers",
        "torch",
        "transformers",
    }


def fastapi_import_allowed(module_path: tuple[str, ...]) -> bool:
    return module_path[:1] == ("bootstrap",) or (
        len(module_path) >= 4
        and module_path[:1] == ("slices",)
        and module_path[2:4] == ("adapters", "api")
    )


def aiogram_import_allowed(module_path: tuple[str, ...]) -> bool:
    return module_path[:1] == ("bootstrap",) or adapter_import_allowed(
        module_path, "telegram"
    )


def embedding_import_allowed(module_path: tuple[str, ...]) -> bool:
    return module_path[:1] == ("bootstrap",) or adapter_import_allowed(
        module_path, "embedding"
    )


def persistence_import_allowed(module_path: tuple[str, ...]) -> bool:
    return module_path[:1] in {
        ("bootstrap",),
        ("persistence",),
    } or adapter_import_allowed(module_path, "persistence")


def adapter_import_allowed(module_path: tuple[str, ...], adapter: str) -> bool:
    return (
        len(module_path) >= 4
        and module_path[:1] == ("slices",)
        and module_path[2:4] == ("adapters", adapter)
    )


def domain_import_allowed(module_path: tuple[str, ...], imported_module: str) -> bool:
    if not imported_module.startswith("second_brain."):
        return is_allowed_standard_library_import(imported_module)

    own_domain_namespace = f"second_brain.slices.{module_path[1]}.domain"
    return is_module_or_descendant(
        imported_module, own_domain_namespace
    ) or is_module_or_descendant(imported_module, "second_brain.shared")


def application_import_allowed(
    module_path: tuple[str, ...], imported_module: str
) -> bool:
    if not imported_module.startswith("second_brain."):
        return is_allowed_standard_library_import(imported_module)

    own_slice_namespace = f"second_brain.slices.{module_path[1]}"
    if any(
        is_module_or_descendant(imported_module, f"{own_slice_namespace}.{layer}")
        for layer in ("application", "domain", "ports")
    ):
        return True
    if is_module_or_descendant(imported_module, "second_brain.shared"):
        return True
    return is_published_contract(imported_module)


def is_module_or_descendant(imported_module: str, namespace: str) -> bool:
    return imported_module == namespace or imported_module.startswith(f"{namespace}.")


def is_published_contract(imported_module: str) -> bool:
    parts = imported_module.split(".")
    return (
        len(parts) >= 5
        and parts[:2] == ["second_brain", "slices"]
        and parts[3:5] == ["application", "contracts"]
    )


def is_allowed_standard_library_import(imported_module: str) -> bool:
    root_module = imported_module.partition(".")[0]
    return root_module in stdlib_module_names and not imports_forbidden_framework(
        imported_module
    )


def imports_forbidden_framework(imported_module: str) -> bool:
    forbidden_roots = (
        "aiogram",
        "fastapi",
        "pydantic",
        "sqlalchemy",
        "celery",
        "dramatiq",
        "openai",
        "anthropic",
        "boto3",
        "os",
        "shutil",
        "tempfile",
        "glob",
        "pathlib",
        "queue",
        "fileinput",
        "zipfile",
        "tarfile",
        "gzip",
        "bz2",
        "lzma",
        "mmap",
        "stat",
        "fnmatch",
        "urllib",
    )
    return imported_module.partition(".")[0] in forbidden_roots
