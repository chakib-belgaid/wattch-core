"""Trace handlers, mirroring pyJoules' handler concept."""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import IO

from .meter import EnergyTrace


class PrintHandler:
    """Print one line per tagged phase and domain."""

    def __init__(self, stream: IO[str] | None = None):
        self.stream = stream or sys.stdout

    def process(self, trace: EnergyTrace) -> None:
        for sample in trace:
            print(
                f"{sample.tag}: {sample.duration_s * 1000:.1f} ms, "
                f"{sample.total_joules:.6f} J",
                file=self.stream,
            )
            for domain, joules in sorted(sample.energy.items()):
                print(f"  {domain}: {joules:.6f} J", file=self.stream)
            for domain, joules in sorted(sample.estimated_energy.items()):
                print(f"  {domain}: {joules:.6f} J (estimated)", file=self.stream)


class CSVHandler:
    """Append long-format rows: tag, duration_s, domain, joules, kind."""

    def __init__(self, path: str):
        self.path = Path(path)

    def process(self, trace: EnergyTrace) -> None:
        new_file = not self.path.exists()
        with self.path.open("a", newline="") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(["tag", "duration_s", "domain", "joules", "kind"])
            for sample in trace:
                for domain, joules in sorted(sample.energy.items()):
                    writer.writerow(
                        [sample.tag, f"{sample.duration_s:.6f}", domain, repr(joules), "measured"]
                    )
                for domain, joules in sorted(sample.estimated_energy.items()):
                    writer.writerow(
                        [sample.tag, f"{sample.duration_s:.6f}", domain, repr(joules), "estimated"]
                    )
