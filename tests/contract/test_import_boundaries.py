import ast
from pathlib import Path


def test_core_has_no_platform_or_database_imports():
    root = Path(__file__).parents[2] / "weavemem" / "core"
    forbidden = {"astrbot", "sqlite3"}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            else:
                continue
            assert not (names & forbidden), f"forbidden import in {path}: {names & forbidden}"


def test_app_has_no_concrete_adapter_or_sqlite_imports():
    root = Path(__file__).parents[2] / "weavemem" / "app"
    forbidden = {"sqlite3", "astrbot", "adapters"}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            else:
                continue
            assert not (names & forbidden), f"forbidden dependency in {path}: {names & forbidden}"
