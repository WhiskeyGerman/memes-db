import ast
import sys
from enum import IntEnum, StrEnum
from pathlib import Path

import pytest

from memes_kernel.values import Number


class Layer(StrEnum):
    DOMAIN = "domain"
    APPLICATION = "application"
    INFRASTRUCTURE = "infrastructure"
    PRESENTATION = "presentation"


class LayerDepth(IntEnum):
    DOMAIN = 0
    APPLICATION = 1
    INFRASTRUCTURE = 2
    PRESENTATION = 3


class ArchitectureRule(StrEnum):
    IMPORT_LEAK = "Нарушена граница слоя"
    CROSS_SERVICE = "Прямой импорт соседнего сервиса"
    DOMAIN_DEPENDENCY = "Домен зависит от сторонней библиотеки"
    NESTED_IMPORT = "Импорт вне уровня модуля"
    BROAD_HANDLER = "Слишком широкий обработчик исключений"
    INTERFACE = "Интерфейс должен наследовать ABC; имя начинается буквой I"


@pytest.fixture
def source_files() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parents[Number.ONE]
    sources = (
        root / "packages" / "kernel" / "src",
        root / "packages" / "platform" / "src",
        *sorted((root / "services").glob("*/src")),
    )
    return tuple(path for source in sources for path in source.rglob("*.py"))


def test_layer_dependencies(source_files: tuple[Path, ...]) -> None:
    depths = {
        Layer.DOMAIN: LayerDepth.DOMAIN,
        Layer.APPLICATION: LayerDepth.APPLICATION,
        Layer.INFRASTRUCTURE: LayerDepth.INFRASTRUCTURE,
        Layer.PRESENTATION: LayerDepth.PRESENTATION,
    }
    for path in source_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        package = next(part for part in path.parts if part.startswith("memes_"))
        layer = next((Layer(part) for part in path.parts if part in depths), Layer.DOMAIN)
        for node in ast.walk(tree):
            imports: list[str] = []
            if isinstance(node, ast.ImportFrom):
                assert node.level == Number.ZERO, path
                imports = [node.module or ""]
            elif isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            for module in imports:
                parts = module.split(".")
                if parts[Number.ZERO].startswith("memes_"):
                    assert not (layer is Layer.DOMAIN and parts[Number.ZERO] == "memes_platform"), (
                        ArchitectureRule.DOMAIN_DEPENDENCY,
                        path,
                        module,
                    )
                    assert parts[Number.ZERO] in {package, "memes_kernel", "memes_platform"}, (
                        ArchitectureRule.CROSS_SERVICE,
                        path,
                        module,
                    )
                    if len(parts) > Number.ONE and parts[Number.ONE] in depths:
                        imported_layer = Layer(parts[Number.ONE])
                        assert depths[imported_layer] <= depths[layer], (
                            ArchitectureRule.IMPORT_LEAK,
                            path,
                            module,
                        )
                elif layer is Layer.DOMAIN:
                    assert parts[Number.ZERO] in sys.stdlib_module_names, (
                        ArchitectureRule.DOMAIN_DEPENDENCY,
                        path,
                        module,
                    )


def test_imports_are_top_level_and_exception_handlers_are_specific(
    source_files: tuple[Path, ...],
) -> None:
    for path in source_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                assert node in tree.body, (ArchitectureRule.NESTED_IMPORT, path, node.lineno)
            if isinstance(node, ast.ExceptHandler):
                assert node.type is not None, (ArchitectureRule.BROAD_HANDLER, path)
                names = {name.id for name in ast.walk(node.type) if isinstance(name, ast.Name)}
                assert not names.intersection({"Exception", "BaseException"}), (
                    ArchitectureRule.BROAD_HANDLER,
                    path,
                )


def test_interfaces_use_named_abc(source_files: tuple[Path, ...]) -> None:
    for path in source_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {base.id for base in node.bases if isinstance(base, ast.Name)}
                assert "Protocol" not in bases, (ArchitectureRule.INTERFACE, path)
                if "ABC" in bases:
                    assert node.name.startswith("I"), (ArchitectureRule.INTERFACE, path, node.name)
