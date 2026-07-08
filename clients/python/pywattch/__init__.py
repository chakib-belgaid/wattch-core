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
from .errors import (
    WattchConnectionError,
    WattchDaemonError,
    WattchError,
    WattchProtocolError,
)
from .meter import EnergyContext, EnergyMeter, EnergySample, EnergyTrace, measure_energy

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_SOCKET_PATH",
    "PROTOCOL_VERSION",
    "default_socket_path",
    "EnergyContext",
    "EnergyMeter",
    "EnergySample",
    "EnergyTrace",
    "RawSample",
    "RawSampleBatch",
    "SourceDescriptor",
    "WattchClient",
    "WattchConnectionError",
    "WattchDaemonError",
    "WattchError",
    "WattchProtocolError",
    "measure_energy",
    "__version__",
]
