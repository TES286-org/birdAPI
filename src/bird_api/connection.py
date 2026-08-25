"""Low-level Unix-socket connection to a BIRD control socket.

This module owns the raw socket, the receive buffer, and the wire-level
send/receive loop. It is responsible for:

* opening a **persistent** AF_UNIX / SOCK_STREAM connection,
* draining the initial ``0001 BIRD <ver> ready.\\n`` banner,
* sending commands and reading reply lines until a FINAL line arrives,
* transparently reconnecting if the socket dies between commands,
* invoking an optional async-message callback for unsolicited ``+`` lines.

It does **not** know about command semantics — see :mod:`bird_api.client`
for that. The :class:`BirdConnection.execute` method returns a
:class:`~bird_api.protocol.CommandResult` which command-specific parsers
turn into structured data.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import re
import select
import socket
import time
from collections.abc import Callable
from typing import Final

from .exceptions import (
    BirdCommandError,
    BirdConnectionError,
    BirdProtocolError,
    BirdTimeoutError,
    BirdValidationError,
)
from .protocol import CommandResult, ReplyLine, assemble_result, parse_line
from .validation import validate_command_line

__all__ = ["BirdConnection", "DEFAULT_SOCKET_PATH", "DEFAULT_TIMEOUT"]

_log = logging.getLogger("bird_api.connection")

# Default control-socket path on Debian (see scripts/bird-protocol-research.md §1.2).
DEFAULT_SOCKET_PATH: Final[str] = "/run/bird/bird.ctl"

# Default per-command timeout in seconds. BIRD itself does not impose a
# timeout, but we want callers to fail loudly rather than hang forever.
DEFAULT_TIMEOUT: float = 30.0

# On connect, the daemon sends a single ``0001 BIRD <ver> ready.\n`` line.
# We tolerate a slightly older/newer version string but we do drain it
# before issuing any commands.
_BANNER_CODE: Final[int] = 1

# Read-only command allowlist for connections created with ``read_only=True``.
# Only inspection commands pass; anything that mutates daemon state
# (configure / enable / disable / restart / reload / dump / down / …) or
# session state is rejected client-side, before a single byte is written to
# the socket. This is deliberately conservative: ``eval`` and ``echo`` are
# excluded too (they are not needed for monitoring and ``eval`` can burn
# daemon CPU on pathological expressions). ``configure status`` is
# read-only introspection, and ``restrict`` only ever *tightens* the
# session's own permissions, so both are allowed.
_READ_ONLY_COMMAND_RE: Final[re.Pattern[str]] = re.compile(
    r"\A(?:show\b|configure\s+status\b|restrict\b)",
)


AsyncCallback = Callable[[str], None]
"""Signature of the optional callback invoked for every ``+``-prefixed async message."""


class BirdConnection:
    """Persistent Unix-socket connection to a running BIRD daemon.

    The connection is opened lazily on the first ``execute()`` call (or
    eagerly if :meth:`connect` is called explicitly). It is reused across
    commands until :meth:`close` is called or the underlying socket dies,
    in which case the next ``execute()`` triggers a reconnect.

    Parameters
    ----------
    socket_path:
        Path to the Unix domain socket file. Defaults to the Debian
        package's ``/run/bird/bird.ctl``.
    timeout:
        Per-command deadline in seconds. A command that does not see its
        FINAL line within this many seconds raises :class:`BirdTimeoutError`.
    on_async:
        Optional callback invoked once for every ``+``-prefixed async
        message. The argument is the message text (without the leading ``+``).
        If the callback raises, the exception propagates out of ``execute``.
    read_only:
        When ``True``, only inspection commands (``show …``,
        ``configure status``, ``restrict``) are allowed through — everything
        else raises :class:`BirdValidationError` before touching the socket.
        This is a client-side allowlist; combine it with the server-side
        ``restrict`` command for defence in depth.

    Example
    -------
    >>> conn = BirdConnection(socket_path="/tmp/bird.ctl")
    >>> conn.connect()
    >>> r = conn.execute("show status")
    >>> r.success, r.final_code
    (True, 13)
    >>> conn.close()
    """

    def __init__(
        self,
        *,
        socket_path: str = DEFAULT_SOCKET_PATH,
        timeout: float = DEFAULT_TIMEOUT,
        on_async: AsyncCallback | None = None,
        read_only: bool = False,
    ) -> None:
        self._socket_path = socket_path
        self._timeout = timeout
        self._on_async = on_async
        self._read_only = read_only
        self._sock: socket.socket | None = None
        # Receive buffer holds bytes that have not yet been split on '\n'.
        self._rx_buf = bytearray()
        # BIRD version reported in the hello banner. Useful for diagnostics.
        self.server_version: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """``True`` if a live socket is currently held."""
        return self._sock is not None

    def connect(self) -> None:
        """Open the socket and drain the hello banner.

        Raises :class:`BirdConnectionError` if the daemon is unreachable
        or the banner does not arrive within the configured timeout.
        """
        if self._sock is not None:
            return  # already connected

        _log.debug("connecting to BIRD socket at %s", self._socket_path)
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)
            sock.connect(self._socket_path)
            sock.setblocking(False)
            self._sock = sock
            self._rx_buf = bytearray()
            self._drain_banner()
        except (OSError, BirdConnectionError, BirdProtocolError) as exc:
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
            self._sock = None
            raise BirdConnectionError(
                f"failed to connect to BIRD control socket {self._socket_path!r}: {exc}",
            ) from exc

    def close(self) -> None:
        """Close the socket if open. Safe to call multiple times."""
        sock = self._sock
        self._sock = None
        self._rx_buf = bytearray()
        self.server_version = None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    def __enter__(self) -> BirdConnection:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def execute(self, command: str, *, timeout: float | None = None) -> CommandResult:
        """Send ``command`` and block until the daemon replies with its FINAL line.

        Parameters
        ----------
        command:
            The command line to send (e.g. ``"show status"``). A trailing
            ``\\n`` is appended internally — callers must not include it.
        timeout:
            Optional override for the per-command timeout in seconds. ``None``
            means use the connection's configured timeout.

        Returns
        -------
        CommandResult
            Decoded response. Use ``.success`` / ``.final_code`` / ``.raw_text``
            / ``.sections`` to inspect it.

        Raises
        ------
        BirdConnectionError
            If the socket died and could not be reconnected.
        BirdCommandError
            If the daemon reported a runtime/parse error (final code in 8xxx/9xxx).
        BirdTimeoutError
            If no FINAL line arrived within the timeout window.
        BirdProtocolError
            If the bytes received do not match the wire protocol.
        BirdValidationError
            If ``read_only=True`` and the command is not an inspection
            command, or the command contains control characters.
        """
        # Injection guard: never let a newline (or any control character)
        # onto the wire through the escape hatch.
        validate_command_line(command)

        if self._read_only and not _READ_ONLY_COMMAND_RE.match(command):
            raise BirdValidationError(
                f"read-only connection rejects command {command!r}",
                field="command",
                value=command,
            )

        if len(command) > 4094:  # CLI_RX_BUF_SIZE - 2
            raise BirdCommandError(
                9000,
                f"Command too long ({len(command)} bytes, max 4094)",
                command=command,
            )

        deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)

        # Ensure we're connected; transparently reconnect on dead socket.
        if self._sock is None:
            self.connect()

        # Send the command in one write. The kernel socket buffer is plenty
        # for any 4094-byte command, so a single non-blocking write suffices.
        payload = (command + "\n").encode("utf-8")
        self._send_all(payload, deadline)

        # Read reply lines until we see a FINAL line.
        lines: list[ReplyLine] = []
        while True:
            line = self._read_line(deadline)
            if line is None:
                # Socket closed cleanly mid-response.
                raise BirdConnectionError(
                    "BIRD daemon closed the connection mid-response",
                )

            if line.kind.name == "ASYNC":
                # Async message: dispatch to callback, do not terminate the response.
                if self._on_async is not None:
                    self._on_async(line.text)
                lines.append(line)
                continue

            lines.append(line)
            if line.kind.name == "FINAL":
                break

        result = assemble_result(command, lines)
        if not result.success:
            raise BirdCommandError(
                result.final_code,
                result.final_text,
                command=command,
            )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _drain_banner(self) -> None:
        """Read the initial ``0001 BIRD <ver> ready.\\n`` banner.

        We do NOT treat the banner as a command response; we just discard it
        (after recording the version string). It is the only FINAL line that
        is not preceded by an explicit command from our side.
        """
        assert self._sock is not None
        deadline = time.monotonic() + self._timeout
        # Read lines until we see the FINAL hello banner line (code 0001).
        # In practice the daemon emits exactly one line.
        while True:
            line = self._read_line(deadline)
            if line is None:
                raise BirdConnectionError("daemon closed connection before sending the hello banner")
            if line.kind.name == "FINAL" and line.code == _BANNER_CODE:
                # Capture the version: 'BIRD 3.1.7 ready.' -> '3.1.7'
                text = line.text
                if text.startswith("BIRD ") and text.endswith(" ready."):
                    self.server_version = text[len("BIRD ") : -len(" ready.")]
                else:
                    self.server_version = text
                _log.debug("BIRD hello banner received, version=%s", self.server_version)
                return
            # Tolerate stray async messages during the banner read by ignoring them.

    def _send_all(self, payload: bytes, deadline: float) -> None:
        """Send all bytes, retrying on EAGAIN/EINTR, reconnecting on socket death."""
        assert self._sock is not None
        sock = self._sock
        sent = 0
        while sent < len(payload):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BirdTimeoutError("timed out while sending command")
                _, writable, _ = select.select([], [sock], [], remaining)
                if not writable:
                    raise BirdTimeoutError("timed out while sending command")
                n = sock.send(payload[sent:])
                if n == 0:
                    raise BirdConnectionError("socket send returned 0 — connection closed")
                sent += n
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EINTR):
                    continue
                # Socket is dead — drop and let the next execute() reconnect.
                self._mark_dead()
                raise BirdConnectionError(f"failed to send command: {exc}") from exc

    def _read_line(self, deadline: float) -> ReplyLine | None:
        """Read one CRLF/LF-terminated line from the socket.

        Returns ``None`` if the peer closed the connection cleanly with no
        more data. Raises :class:`BirdConnectionError` on a hard socket error
        or :class:`BirdTimeoutError` if the deadline passes without a full line.
        """
        assert self._sock is not None
        sock = self._sock

        while True:
            # Fast path: do we already have a complete line in the buffer?
            nl_idx = self._rx_buf.find(b"\n")
            if nl_idx >= 0:
                raw = bytes(self._rx_buf[:nl_idx])
                del self._rx_buf[: nl_idx + 1]
                return parse_line(raw)

            # No complete line yet — read more bytes.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BirdTimeoutError("timed out while waiting for BIRD reply")

            # Always also watch for the socket being exceptional/closed.
            try:
                readable, _, exceptional = select.select([sock], [], [sock], remaining)
            except (OSError, ValueError) as exc:
                self._mark_dead()
                raise BirdConnectionError(f"select() failed: {exc}") from exc

            if exceptional:
                self._mark_dead()
                raise BirdConnectionError("socket reported an exceptional condition")

            if not readable:
                raise BirdTimeoutError("timed out while waiting for BIRD reply")

            try:
                chunk = sock.recv(4096)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EINTR):
                    continue
                self._mark_dead()
                raise BirdConnectionError(f"recv() failed: {exc}") from exc

            if not chunk:
                # Peer closed the connection. If we have leftover bytes,
                # they're a partial line — drop them and signal EOF.
                if self._rx_buf:
                    _log.debug(
                        "discarding %d leftover bytes after peer close",
                        len(self._rx_buf),
                    )
                    self._rx_buf = bytearray()
                return None

            self._rx_buf.extend(chunk)

    def _mark_dead(self) -> None:
        """Drop the current socket so the next execute() reconnects."""
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        self._sock = None
        self._rx_buf = bytearray()
        self.server_version = None

    # Allow ``with BirdConnection(...) as conn:`` to also be used as
    # ``with conn:``.
    @property
    def socket_path(self) -> str:
        return self._socket_path

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def read_only(self) -> bool:
        """Whether this connection enforces the read-only command allowlist."""
        return self._read_only


# Re-exported for convenience at the module level (avoids one level of
# attribute access in user code that does ``from bird_api.connection import ...``).
def is_socket_alive(path: str) -> bool:
    """Return ``True`` if a Unix socket exists at ``path`` and accepts connections.

    Useful as a sanity check before constructing a :class:`BirdConnection`.
    """
    if not os.path.exists(path):
        return False
    if not stat_is_socket(path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(path)
        return True
    except OSError:
        return False


def stat_is_socket(path: str) -> bool:
    try:
        mode = os.stat(path, follow_symlinks=False).st_mode
        return (mode & 0o170000) != 0o140000  # S_IFSOCK
    except OSError:
        return False
