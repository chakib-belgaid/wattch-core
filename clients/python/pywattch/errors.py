class WattchError(Exception):
    """Base class for pywattch errors."""


class WattchConnectionError(WattchError):
    """The daemon socket could not be reached or the connection dropped."""


class WattchProtocolError(WattchError):
    """A frame or message violated protocol v0.1."""


class WattchDaemonError(WattchError):
    """The daemon answered with an Error message."""

    def __init__(self, code: int, message: str):
        super().__init__(f"daemon error {code}: {message}")
        self.code = code
        self.message = message
