"""Asyncio-based Unix-socket connection to a BIRD control socket.

This is the asynchronous counterpart of :class:`~bird_api.connection.BirdConnection`
with identical semantics:

* lazy connect on the first ``execute()`` (or eager via :meth:`connect`),
* the ``0001 BIRD <ver> ready.\\n`` banner is drained on connect,
* commands are validated for single-line framing and against the shared
  read-only allowlist before anything is written,
* reply lines are read until a FINAL line arrives,
* ``+``-prefixed async messages are dispatched to an optional callback —
  plain functions **and** coroutine functions are both supported,
* a dead socket triggers a transparent reconnect on the next ``execute()``.

Two asyncio-specific behaviours:

* Concurrent ``execute()`` calls are serialised with an
  :class:`asyncio.Lock` — the daemon's control protocol is strictly
  request/response on one connection, so interleaving sends would corrupt
  the reply stream.
* On timeout the connection is dropped (not just abandoned): a partial
  response in the stream would otherwise desynchronise the next command.

Example
-------
    >>> import asyncio
    >>> from bird_api import AsyncBirdClient
    >>> async def main():
    ...     async with AsyncBirdClient(socket_path="/tmp/bird.ctl") as client:
    ...         status = await client.show_status()
    ...         print(status.router_id)
    >>> asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable

from .connection import DEFAULT_SOCKET_PATH, DEFAULT_TIMEOUT
from .exceptions import (
    BirdCommandError,
    BirdConnectionError,
    BirdProtocolError,
    BirdTimeoutError,
    BirdValidationError,
)
from .protocol import CommandResult, ReplyLine, ReplyLineKind, assemble_result, parse_line
from .validation import is_read_only_command, validate_command_line

__all__ = [
    "AsyncBirdConnection",
    "AsyncCallback",
    "DEFAULT_SOCKET_PATH",
    "DEFAULT_TIMEOUT",
]

_log = logging.getLogger("bird_api.async_connection")

# Banner code drained on connect (``0001 BIRD <ver> ready.``).
_BANNER_CODE = 1

# Callback: either a plain callable or a coroutine function.
AsyncCallback = Callable[[str], None] | Callable[[str], Awaitable[None]]
"""Signature of the optional async-message callback (sync or coroutine)."""


class AsyncBirdConnection:
    """Persistent asyncio connection to a running BIRD daemon.

    Parameters mirror :class:`~bird_api.connection.BirdConnection`:

    * ``socket_path`` — Unix socket path (default ``/run/bird/bird.ctl``).
    * ``timeout`` — per-command deadline in seconds.
    * ``on_async`` — optional callback for ``+``-prefixed messages; both
      ``def cb(text)`` and ``async def cb(text)`` are accepted.
    * ``read_only`` — reject mutating commands client-side (shared
      allowlist with the sync connection).
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
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self.server_version: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """``True`` if a live stream is currently held."""
        return self._writer is not None

    async def connect(self) -> None:
        """Open the socket and drain the hello banner.

        Raises :class:`BirdConnectionError` if the daemon is unreachable
        or the banner does not arrive within the configured timeout.
        """
        if self._writer is not None:
            return  # already connected

        _log.debug("connecting to BIRD socket at %s", self._socket_path)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self._socket_path),
                timeout=self._timeout,
            )
        except (TimeoutError, OSError) as exc:
            raise BirdConnectionError(
                f"failed to connect to BIRD control socket {self._socket_path!r}: {exc}",
            ) from exc

        self._reader = reader
        self._writer = writer
        try:
            await asyncio.wait_for(self._drain_banner(), timeout=self._timeout)
        except (TimeoutError, BirdConnectionError, BirdProtocolError, OSError) as exc:
            await self._mark_dead()
            raise BirdConnectionError(
                f"failed to connect to BIRD control socket {self._socket_path!r}: {exc}",
            ) from exc

    async def close(self) -> None:
        """Close the stream if open. Safe to call multiple times."""
        await self._mark_dead()

    async def __aenter__(self) -> AsyncBirdConnection:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def execute(self, command: str, *, timeout: float | None = None) -> CommandResult:
        """Send ``command`` and await the daemon's FINAL line.

        Semantics match :meth:`BirdConnection.execute
        <bird_api.connection.BirdConnection.execute>`; the differences are
        that it is a coroutine, concurrent calls are serialised, and a
        timeout drops the connection (a partial response would otherwise
        desynchronise the following command).

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
        validate_command_line(command)

        if self._read_only and not is_read_only_command(command):
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

        # Serialise concurrent executes: one request/response stream.
        async with self._lock:
            if self._writer is None:
                await self.connect()

            assert self._reader is not None and self._writer is not None
            effective = self._timeout if timeout is None else timeout
            try:
                return await asyncio.wait_for(
                    self._execute_locked(command),
                    timeout=effective,
                )
            except TimeoutError as exc:
                # A partial response may sit in the stream — drop the socket
                # so the next execute() reconnects cleanly.
                await self._mark_dead()
                raise BirdTimeoutError("timed out while waiting for BIRD reply") from exc
            except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
                await self._mark_dead()
                raise BirdConnectionError(f"connection to BIRD daemon failed: {exc}") from exc

    async def _execute_locked(self, command: str) -> CommandResult:
        """Send + receive under the connection lock. No timeout handling."""
        assert self._reader is not None and self._writer is not None

        self._writer.write((command + "\n").encode("utf-8"))
        await self._writer.drain()

        lines: list[ReplyLine] = []
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise BirdConnectionError("BIRD daemon closed the connection mid-response")
            line = parse_line(raw.rstrip(b"\n"))
            if line.kind is ReplyLineKind.ASYNC:
                if self._on_async is not None:
                    callback_result = self._on_async(line.text)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                lines.append(line)
                continue
            lines.append(line)
            if line.kind is ReplyLineKind.FINAL:
                break

        result = assemble_result(command, lines)
        if not result.success:
            raise BirdCommandError(result.final_code, result.final_text, command=command)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _drain_banner(self) -> None:
        """Read and discard the initial ``0001 BIRD <ver> ready.\\n`` banner."""
        assert self._reader is not None
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise BirdConnectionError("daemon closed connection before sending the hello banner")
            line = parse_line(raw.rstrip(b"\n"))
            if line.kind is ReplyLineKind.FINAL and line.code == _BANNER_CODE:
                text = line.text
                if text.startswith("BIRD ") and text.endswith(" ready."):
                    self.server_version = text[len("BIRD ") : -len(" ready.")]
                else:
                    self.server_version = text
                _log.debug("BIRD hello banner received, version=%s", self.server_version)
                return
            # Tolerate stray async messages during the banner read.

    async def _mark_dead(self) -> None:
        """Drop the current stream so the next execute() reconnects."""
        writer, self._writer, self._reader = self._writer, None, None
        self.server_version = None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

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
