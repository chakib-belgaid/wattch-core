"""pywattch: energy measurement for Python, backed by a Wattch daemon.

The updated pyJoules-style API (EnergyMeter, measure_energy, EnergyContext)
talks to a local Wattch daemon (wattch-powerd or wattch-testd) over a Unix
socket. No elevated privileges are needed on the Python side; the daemon
owns the hardware boundary.
"""

from ._messages import (
    PROTOCOL_VERSION,
    RawSample,
    RawSampleBatch,
    SourceDescriptor,
)
from .client import DEFAULT_SOCKET_PATH, WattchClient, default_socket_path
from .energytest import energy_test
from .errors import (
    WattchConnectionError,
    WattchDaemonError,
    WattchError,
    WattchProtocolError,
)
from .handlers import CSVHandler, PrintHandler
from .meter import (
    EnergyContext,
    EnergyMeter,
    EnergySample,
    EnergyTrace,
    domains_of,
    measure_energy,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_SOCKET_PATH",
    "PROTOCOL_VERSION",
    "default_socket_path",
    "CSVHandler",
    "EnergyContext",
    "EnergyMeter",
    "EnergyProfiler",
    "EnergySample",
    "EnergyTrace",
    "PrintHandler",
    "RawSample",
    "RawSampleBatch",
    "SourceDescriptor",
    "WattchClient",
    "WattchConnectionError",
    "WattchDaemonError",
    "WattchError",
    "WattchProtocolError",
    "domains_of",
    "energy_test",
    "measure_energy",
    "__version__",
]


def __getattr__(name: str):
    """Lazily import EnergyProfiler so bare `import pywattch` doesn't need
    the optional `pyinstrument` dependency (see the `profiler` extra)."""
    if name == "EnergyProfiler":
        from .profiler import EnergyProfiler

        return EnergyProfiler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
