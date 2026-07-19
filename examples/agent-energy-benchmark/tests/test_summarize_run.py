from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import summarize_run  # noqa: E402


class SummarizeRunTest(unittest.TestCase):
    def test_cli_window_starts_before_child_spawn(self) -> None:
        batch = SimpleNamespace(
            events=[
                SimpleNamespace(kind="TARGET_SPAWN", monotonic_ns=100),
                SimpleNamespace(kind="TARGET_EXEC", monotonic_ns=120),
                SimpleNamespace(kind="TARGET_EXIT", monotonic_ns=200),
            ]
        )

        selected = summarize_run.select_window([batch])

        self.assertEqual(selected, ("TARGET_SPAWN", "TARGET_EXIT", 100, 200))

    def test_mcp_window_remains_supported(self) -> None:
        batch = SimpleNamespace(
            events=[
                SimpleNamespace(kind="WATTCH_WINDOW_START", monotonic_ns=300),
                SimpleNamespace(kind="WATTCH_WINDOW_END", monotonic_ns=400),
            ]
        )

        selected = summarize_run.select_window([batch])

        self.assertEqual(
            selected,
            ("WATTCH_WINDOW_START", "WATTCH_WINDOW_END", 300, 400),
        )


if __name__ == "__main__":
    unittest.main()
