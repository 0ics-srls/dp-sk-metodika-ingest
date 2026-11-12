"""The test suite must COLLECT inside the ProcMon test container (phase 78/79 defect): the container holds this repo only, so a test module that does
path arithmetic like ``Path(__file__).parents[4]`` at import time fails the whole collection.

* every module under ``tests/`` is imported from a copy in a temporary directory (nothing of the surrounding tree is next to it);
* the source of ``tests/`` holds no subscript of ``.parents`` outside a function body.
"""
from __future__ import annotations

import ast
import importlib.util
import shutil
import sys
from pathlib import Path

_SKIP_IMPORT = {"test_collectable.py"}


def _tests_dir() -> Path:
    return Path(__file__).resolve().parent


def module_level_parents(source: str) -> list[int]:
    """Line numbers of ``<expr>.parents[...]`` that run at import time (module or class body, not inside a def or lambda)."""
    hits: list[int] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            return
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
            hits.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(source))
    return hits


def test_the_scanner_flags_import_time_parents_and_allows_function_bodies() -> None:
    assert module_level_parents("from pathlib import Path\nROOT = Path(__file__).resolve().parents[4]\n") == [2]
    assert module_level_parents("class T:\n    root = Path(__file__).parents[1]\n") == [2]
    assert module_level_parents("def root():\n    return Path(__file__).parents[4]\n") == []
    assert module_level_parents("f = lambda: Path(__file__).parents[1]\n") == []
    assert module_level_parents("ROOT = Path(__file__).resolve().parent.parent\n") == []


def test_no_test_module_does_path_arithmetic_with_parents_at_import_time() -> None:
    offenders = {
        str(path.relative_to(_tests_dir())): lines
        for path in sorted(_tests_dir().rglob("*.py"))
        if (lines := module_level_parents(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}


def test_every_test_module_imports_from_a_copy_in_a_temp_dir(tmp_path: Path) -> None:
    copy = tmp_path / "tests"
    shutil.copytree(_tests_dir(), copy, ignore=shutil.ignore_patterns("__pycache__"))
    failures: dict[str, str] = {}
    sys.path.insert(0, str(copy))
    try:
        for index, path in enumerate(sorted(copy.rglob("*.py"))):
            if path.name in _SKIP_IMPORT or path.name == "__init__.py":
                continue
            spec = importlib.util.spec_from_file_location(f"collect_probe_{index}_{path.stem}", path)
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as error:  # noqa: BLE001 - every import failure is reported by module
                failures[str(path.relative_to(copy))] = f"{type(error).__name__}: {error}"
    finally:
        sys.path.remove(str(copy))

    assert failures == {}


def test_the_integration_module_names_its_migrations_through_a_function() -> None:
    source = (_tests_dir() / "integration" / "test_ingest_postgres.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert any(isinstance(node, ast.FunctionDef) and node.name == "migrations_dir" for node in tree.body)
    assert not any(isinstance(node, ast.Assign) and "migrations" in ast.unparse(node).lower() for node in tree.body)
