"""Exceptions raised by the bird_api library.

All exceptions inherit from :class:`BirdError` so callers can catch any
library-level error with a single ``except BirdError``.
"""

from __future__ import annotations


class BirdError(Exception):
    """Base class for all bird_api errors."""


class BirdConnectionError(BirdError):
    """Raised when the Unix socket connection cannot be established or is lost.

    Common causes:
      * The BIRD daemon is not running on the configured socket path.
      * A stale socket file exists with no listener (``connect`` returns ECONNREFUSED).
      * The daemon shut down mid-conversation and the socket was reset.
    """


class BirdProtocolError(BirdError):
    """Raised when bytes received from the daemon do not match the wire protocol.

    This almost always indicates either a bug in bird_api or an unsupported
    BIRD version. The ``raw_line`` attribute carries the offending bytes.
    """

    def __init__(self, message: str, *, raw_line: bytes | None = None) -> None:
        super().__init__(message)
        self.raw_line = raw_line


class BirdCommandError(BirdError):
    """Raised when the daemon reports a runtime error for an executed command.

    BIRD reports runtime/parse errors with a final reply code in the
    ``8xxx`` / ``9xxx`` range. The ``code`` attribute is that numeric code,
    and ``message`` is the daemon's error text (e.g. ``"Network not found"``
    for code ``8001``).
    """

    def __init__(self, code: int, message: str, *, command: str | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.command = command


class BirdTimeoutError(BirdError):
    """Raised when a command does not complete within the configured deadline."""


class BirdParseError(BirdError):
    """Raised when a command's structured parser cannot make sense of the response.

    The ``result`` attribute carries the :class:`~bird_api.protocol.CommandResult`
    that was being parsed, so callers can still inspect the raw text. This
    usually indicates the daemon's response format has drifted from what the
    parser expects; users can fall back to the raw text while waiting for
    a library update.
    """

    def __init__(self, message: str, *, raw_text: str | None = None) -> None:
        super().__init__(message)
        self.raw_text = raw_text
