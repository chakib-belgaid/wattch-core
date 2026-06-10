"""Find energy tests in a directory tree."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import ATTR_BUDGET, ATTR_MARK, ATTR_REPEAT

FILE_PATTERNS = ("energy_test_*.py", "*_energy_test.py")
FUNCTION_PREFIX = "energy_test_"

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox"}


@dataclass(frozen=True)
class DiscoveredTest:
    test_id: str
    """``<file relative to root>::<function name>``"""
    file: str
    name: str
    line: int
    budget_joules: float | None
    repeat: int
    function: Callable

    def to_dict(self) -> dict:
        return {
            "id": self.test_id,
            "file": self.file,
            "name": self.name,
            "line": self.line,
            "budget_joules": self.budget_joules,
            "repeat": self.repeat,
        }


@dataclass(frozen=True)
class DiscoveryError:
    file: str
    message: str

    def to_dict(self) -> dict:
        return {"file": self.file, "message": self.message}


def _candidate_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for pattern in FILE_PATTERNS:
        for path in root.rglob(pattern):
            if not any(part in _SKIP_DIRS for part in path.parts):
                files.add(path)
    return sorted(files)


def _import_module(path: Path):
    digest = hashlib.sha1(str(path).encode()).hexdigest()[:12]
    module_name = f"_wattch_energy_test_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def discover_tests(root: str) -> tuple[list[DiscoveredTest], list[DiscoveryError]]:
    """Import matching files under `root` and collect their energy tests."""
    root_path = Path(root).resolve()
    tests: list[DiscoveredTest] = []
    errors: list[DiscoveryError] = []

    sys.path.insert(0, str(root_path))
    try:
        for path in _candidate_files(root_path):
            relative = str(path.relative_to(root_path))
            try:
                module = _import_module(path)
            except Exception as exc:
                errors.append(DiscoveryError(file=relative, message=str(exc)))
                continue

            for name, obj in sorted(vars(module).items()):
                if not callable(obj) or inspect.isclass(obj):
                    continue
                marked = getattr(obj, ATTR_MARK, False)
                named = name.startswith(FUNCTION_PREFIX)
                if not (marked or named):
                    continue
                # Only collect functions defined in that file, not imports.
                if getattr(obj, "__module__", None) != module.__name__:
                    continue
                try:
                    line = inspect.getsourcelines(obj)[1]
                except (OSError, TypeError):
                    line = 0
                tests.append(
                    DiscoveredTest(
                        test_id=f"{relative}::{name}",
                        file=relative,
                        name=name,
                        line=line,
                        budget_joules=getattr(obj, ATTR_BUDGET, None),
                        repeat=max(int(getattr(obj, ATTR_REPEAT, 1)), 1),
                        function=obj,
                    )
                )
    finally:
        sys.path.remove(str(root_path))

    return tests, errors
