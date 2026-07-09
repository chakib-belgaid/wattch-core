"""Top-level `import pywattch` surface: re-exports and lazy EnergyProfiler."""

import subprocess
import sys

import pywattch


def test_energy_test_is_exported():
    assert pywattch.energy_test is pywattch.energytest.energy_test


def test_handlers_are_exported():
    from pywattch.handlers import CSVHandler, PrintHandler

    assert pywattch.PrintHandler is PrintHandler
    assert pywattch.CSVHandler is CSVHandler


def test_domains_of_is_exported():
    from pywattch.meter import domains_of

    assert pywattch.domains_of is domains_of


def test_energy_profiler_is_lazily_exported():
    from pywattch.profiler import EnergyProfiler

    assert pywattch.EnergyProfiler is EnergyProfiler


def test_unknown_attribute_still_raises_attribute_error():
    import pytest

    with pytest.raises(AttributeError):
        pywattch.not_a_real_export


def test_all_new_names_are_in_dunder_all():
    for name in ("energy_test", "PrintHandler", "CSVHandler", "domains_of", "EnergyProfiler"):
        assert name in pywattch.__all__


def test_dir_includes_lazily_exported_names():
    # The module-level __getattr__ hook hides EnergyProfiler from a default
    # dir(); the __dir__ hook must surface every __all__ name for tooling.
    listing = dir(pywattch)
    assert "EnergyProfiler" in listing
    assert set(pywattch.__all__) <= set(listing)


def test_import_pywattch_works_without_pyinstrument():
    """Bare `import pywattch` must not require the optional profiler extra.

    Run in a subprocess with pyinstrument hidden from the import system, so
    this doesn't depend on (or disturb) whether it's actually installed in
    the current environment.
    """
    script = (
        "import sys\n"
        "sys.modules['pyinstrument'] = None\n"  # None makes import raise ImportError
        "import pywattch\n"
        "assert 'pywattch.profiler' not in sys.modules, "
        "'import pywattch must not eagerly import profiler'\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
