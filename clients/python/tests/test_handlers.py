import csv
import io

from pywattch.handlers import CSVHandler, PrintHandler
from pywattch.meter import EnergySample, EnergyTrace


def trace() -> EnergyTrace:
    return EnergyTrace(
        samples=(
            EnergySample(
                tag="phase1",
                duration_s=0.5,
                energy={"cpu/package/0/energy": 1.25},
                estimated_energy={"gpu/0/power": 0.75},
            ),
            EnergySample(
                tag="phase2",
                duration_s=0.25,
                energy={"cpu/package/0/energy": 0.5},
            ),
        )
    )


def test_print_handler_writes_tags_and_domains():
    out = io.StringIO()
    PrintHandler(stream=out).process(trace())
    text = out.getvalue()
    assert "phase1: 500.0 ms, 1.250000 J" in text
    assert "cpu/package/0/energy: 1.250000 J" in text
    assert "gpu/0/power: 0.750000 J (estimated)" in text
    assert "phase2" in text


def test_csv_handler_appends_long_format_rows(tmp_path):
    path = tmp_path / "energy.csv"
    handler = CSVHandler(str(path))
    handler.process(trace())
    handler.process(trace())  # appending keeps a single header

    with path.open() as fh:
        rows = list(csv.reader(fh))

    assert rows[0] == ["tag", "duration_s", "domain", "joules", "kind"]
    body = rows[1:]
    assert len(body) == 6
    assert body[0] == ["phase1", "0.500000", "cpu/package/0/energy", "1.25", "measured"]
    assert body[1] == ["phase1", "0.500000", "gpu/0/power", "0.75", "estimated"]
