"""High-level :class:`BirdClient` — the primary entry point for upper-layer code.

A :class:`BirdClient` owns a :class:`~bird_api.connection.BirdConnection`
(persistent Unix socket) and exposes one method per supported BIRD command
family. Each method:

1. Builds the wire command string (with optional arguments).
2. Calls :meth:`BirdConnection.execute` to send it and collect the response.
3. Dispatches the response to the matching parser, which returns a typed
   :class:`~bird_api.models.ParsedResponse` carrying both structured fields
   and the original raw text.

The connection is opened lazily on the first command and reused across
all subsequent commands until :meth:`close` is called. If the underlying
socket dies (e.g. the daemon was restarted), the next command triggers a
transparent reconnect.

Example
-------
    >>> from bird_api import BirdClient
    >>> with BirdClient(socket_path="/tmp/bird.ctl") as client:
    ...     status = client.show_status()
    ...     print(status.router_id, status.bird_version)
    ...     for row in client.show_protocols().rows:
    ...         print(row.name, row.state)
    ...     for entry in client.show_route().entries:
    ...         print(entry.prefix, entry.protocol)
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .connection import DEFAULT_SOCKET_PATH, DEFAULT_TIMEOUT, AsyncCallback, BirdConnection
from .models import (
    ActionResult,
    ConfigResult,
    EvalResult,
    InterfacesResult,
    InterfacesSummaryResult,
    MemoryInfo,
    ProtocolsAllResult,
    ProtocolsResult,
    RouteResult,
    Status,
    SymbolsResult,
)
from .parsers import (
    parse_action,
    parse_configure,
    parse_eval,
    parse_interfaces,
    parse_interfaces_summary,
    parse_memory,
    parse_protocols,
    parse_protocols_all,
    parse_route,
    parse_status,
    parse_symbols,
)
from .protocol import CommandResult

__all__ = ["BirdClient", "DEFAULT_SOCKET_PATH", "DEFAULT_TIMEOUT"]


class BirdClient:
    """High-level BIRD control-socket client.

    Parameters
    ----------
    socket_path:
        Path to the Unix domain socket. Defaults to ``/run/bird/bird.ctl``.
    timeout:
        Per-command timeout in seconds.
    on_async:
        Optional callback for unsolicited ``+``-prefixed messages (see
        :class:`~bird_api.connection.BirdConnection` for details).
    """

    def __init__(
        self,
        *,
        socket_path: str = DEFAULT_SOCKET_PATH,
        timeout: float = DEFAULT_TIMEOUT,
        on_async: AsyncCallback | None = None,
    ) -> None:
        self._conn = BirdConnection(
            socket_path=socket_path,
            timeout=timeout,
            on_async=on_async,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> BirdClient:
        """Open the socket eagerly. Returns self for chaining."""
        self._conn.connect()
        return self

    def close(self) -> None:
        """Close the underlying socket. Safe to call multiple times."""
        self._conn.close()

    def __enter__(self) -> BirdClient:
        self._conn.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Read-only ``show`` commands
    # ------------------------------------------------------------------

    def show_status(self) -> Status:
        """``show status`` — daemon version, router ID, uptime, state."""
        result = self._conn.execute("show status")
        return parse_status(result)

    def show_memory(self) -> MemoryInfo:
        """``show memory`` — memory-usage table per resource kind."""
        result = self._conn.execute("show memory")
        return parse_memory(result)

    def show_protocols(self) -> ProtocolsResult:
        """``show protocols`` — one-line summary per configured protocol."""
        result = self._conn.execute("show protocols")
        return parse_protocols(result)

    def show_protocols_all(self) -> ProtocolsAllResult:
        """``show protocols all`` — full per-protocol verbose block."""
        result = self._conn.execute("show protocols all")
        return parse_protocols_all(result)

    def show_route(self, *, all: bool = False) -> RouteResult:
        """``show route [all]`` — all routes from the default table.

        Pass ``all=True`` for the verbose attribute dump.
        """
        cmd = "show route" + (" all" if all else "")
        result = self._conn.execute(cmd)
        return parse_route(result)

    def show_route_prefix(self, prefix: str, *, all: bool = False) -> RouteResult:
        """``show route <prefix> [all]`` — routes for a specific network."""
        cmd = f"show route {prefix}" + (" all" if all else "")
        result = self._conn.execute(cmd)
        return parse_route(result)

    def show_route_for(self, address: str, *, all: bool = False) -> RouteResult:
        """``show route for <ip> [all]`` — routes that would be used to reach <address>."""
        cmd = f"show route for {address}" + (" all" if all else "")
        result = self._conn.execute(cmd)
        return parse_route(result)

    def show_route_in(self, prefix: str, *, all: bool = False) -> RouteResult:
        """``show route in <prefix> [all]`` — routes contained within <prefix>."""
        cmd = f"show route in {prefix}" + (" all" if all else "")
        result = self._conn.execute(cmd)
        return parse_route(result)

    def show_route_table(self, table: str, *, all: bool = False) -> RouteResult:
        """``show route table <name> [all]`` — routes from a named table."""
        cmd = f"show route table {table}" + (" all" if all else "")
        result = self._conn.execute(cmd)
        return parse_route(result)

    def show_route_protocol(self, protocol: str, *, all: bool = False) -> RouteResult:
        """``show route protocol <name> [all]`` — routes learned from a specific protocol."""
        cmd = f"show route protocol {protocol}" + (" all" if all else "")
        result = self._conn.execute(cmd)
        return parse_route(result)

    def show_route_stats(self) -> RouteResult:
        """``show route stats`` — per-table and total route counts."""
        result = self._conn.execute("show route stats")
        return parse_route(result)

    def show_symbols(self, *, table: bool = False) -> SymbolsResult:
        """``show symbols [table]`` — all symbols (or only routing tables)."""
        cmd = "show symbols" + (" table" if table else "")
        result = self._conn.execute(cmd)
        return parse_symbols(result)

    def show_interfaces(self) -> InterfacesResult:
        """``show interfaces`` — full per-interface block."""
        result = self._conn.execute("show interfaces")
        return parse_interfaces(result)

    def show_interfaces_summary(self) -> InterfacesSummaryResult:
        """``show interfaces summary`` — one-line summary per interface."""
        result = self._conn.execute("show interfaces summary")
        return parse_interfaces_summary(result)

    def eval(self, expr: str) -> EvalResult:
        """``eval <expr>`` — evaluate a BIRD filter expression.

        Use ``eval("1+2")`` to compute arithmetic; pass a quoted filter
        expression to evaluate complex filters. The daemon's response
        text becomes ``EvalResult.value``.
        """
        result = self._conn.execute(f"eval {expr}")
        return parse_eval(result)

    # ------------------------------------------------------------------
    # Configuration commands
    # ------------------------------------------------------------------

    def configure(
        self,
        file: str,
        *,
        timeout: int | None = None,
        soft: bool = False,
        check: bool = False,
    ) -> ConfigResult:
        """Apply (or check) a BIRD configuration file.

        Parameters
        ----------
        file:
            Path to the configuration file. The wire command will quote
            this with double quotes per BIRD's lexer rules.
        timeout:
            If given, schedules an automatic undo after this many seconds;
            the daemon will revert to the previous config unless
            :meth:`configure_confirm` is called before the timer expires.
        soft:
            If ``True``, perform a "soft" reconfiguration that preserves
            runtime state where possible.
        check:
            If ``True``, only validate the file — do NOT apply it.
        """
        # Per BIRD's lexer, the file path MUST be a double-quoted string.
        # Internal double quotes inside the path are not supported by BIRD;
        # we don't try to escape them.
        quoted = f'"{file}"'
        if check:
            cmd = f"configure check {quoted}"
        else:
            soft_kw = "soft " if soft else ""
            if timeout is not None:
                cmd = f"configure {soft_kw}{quoted} timeout {timeout}"
            else:
                cmd = f"configure {soft_kw}{quoted}"
        result = self._conn.execute(cmd)
        return parse_configure(result)

    def configure_status(self) -> ConfigResult:
        """``configure status`` — current reconfiguration state."""
        result = self._conn.execute("configure status")
        return parse_configure(result)

    def configure_confirm(self) -> ConfigResult:
        """``configure confirm`` — confirm a pending reconfiguration."""
        result = self._conn.execute("configure confirm")
        return parse_configure(result)

    def configure_undo(self) -> ConfigResult:
        """``configure undo`` — revert to the previous configuration."""
        result = self._conn.execute("configure undo")
        return parse_configure(result)

    # ------------------------------------------------------------------
    # Per-protocol actions
    # ------------------------------------------------------------------

    def enable(self, pattern: str = "all") -> ActionResult:
        """``enable <pattern>`` — enable matching protocols."""
        result = self._conn.execute(f"enable {pattern}")
        return parse_action(result)

    def disable(self, pattern: str = "all") -> ActionResult:
        """``disable <pattern>`` — disable matching protocols."""
        result = self._conn.execute(f"disable {pattern}")
        return parse_action(result)

    def restart(self, pattern: str = "all") -> ActionResult:
        """``restart <pattern>`` — restart matching protocols."""
        result = self._conn.execute(f"restart {pattern}")
        return parse_action(result)

    def reload(self, pattern: str = "all", *, direction: str = "both") -> ActionResult:
        """``reload [in|out] <pattern>`` — reload routes for matching protocols.

        BIRD 3.x rejects ``reload <pattern>`` (it requires ``in`` or ``out``).
        The default ``direction="both"`` issues ``reload <pattern>`` which
        may fail on BIRD 3.x; use ``direction="in"`` or ``direction="out"``
        for compatibility with BIRD 3.x.
        """
        if direction not in ("both", "in", "out"):
            raise ValueError(f"direction must be 'both', 'in', or 'out', got {direction!r}")
        cmd = f"reload {pattern}" if direction == "both" else f"reload {direction} {pattern}"
        result = self._conn.execute(cmd)
        return parse_action(result)

    def dump(self, kind: str, file: str) -> ActionResult:
        """``dump <kind> "<file>"`` — write a runtime dump to <file>.

        ``kind`` must be one of: ``resources``, ``sockets``, ``ao keys``,
        ``events``, ``interfaces``, ``neighbors``, ``attributes``,
        ``routes``, ``protocols``, ``filter all``.
        """
        valid = {
            "resources", "sockets", "ao keys", "events", "interfaces",
            "neighbors", "attributes", "routes", "protocols", "filter all",
        }
        if kind not in valid:
            raise ValueError(
                f"invalid dump kind {kind!r}; must be one of {sorted(valid)!r}",
            )
        cmd = f'dump {kind} "{file}"'
        result = self._conn.execute(cmd)
        return parse_action(result)

    # ------------------------------------------------------------------
    # Session control
    # ------------------------------------------------------------------

    def restrict(self) -> ActionResult:
        """``restrict`` — mark the current session as read-only.

        Subsequent privileged commands (configure, enable, disable,
        restart, dump, etc.) will fail with code 8007 (Access denied).
        """
        # `restrict` always returns code 0016 (which is < 8000, so it is
        # treated as success by the connection layer).
        result = self._conn.execute("restrict")
        return parse_action(result)

    def down(self) -> ActionResult:
        """``down`` — request a clean daemon shutdown.

        The daemon will exit shortly after this command; subsequent
        commands will fail with :class:`~bird_api.exceptions.BirdConnectionError`.
        """
        result = self._conn.execute("down")
        return parse_action(result)

    def graceful_restart(self) -> ActionResult:
        """``graceful restart`` — request a graceful restart."""
        result = self._conn.execute("graceful restart")
        return parse_action(result)

    # ------------------------------------------------------------------
    # Escape hatch — for commands not yet wrapped by a method.
    # ------------------------------------------------------------------

    def raw(self, command: str) -> CommandResult:
        """Run an arbitrary BIRD command and return the unstructured result.

        Use this for commands the wrapper doesn't yet expose; it preserves
        the full :class:`~bird_api.protocol.CommandResult` (with ``.sections``,
        ``.raw_text``, ``.async_messages``, ``.final_code``, ``.success``,
        ``.error_message``) for the caller to parse manually.

        Raises :class:`~bird_api.exceptions.BirdCommandError` if the daemon
        reports a runtime/parse error.
        """
        return self._conn.execute(command)

    def raw_lines(self, command: str) -> Iterator[str]:
        """Like :meth:`raw` but yields only the per-line text (no protocol prefix).

        Useful for ad-hoc streaming-style inspection of new commands.
        """
        result = self._conn.execute(command)
        yield from result.raw_lines

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def server_version(self) -> str | None:
        """BIRD version reported in the hello banner (``None`` if not connected)."""
        return self._conn.server_version

    @property
    def is_connected(self) -> bool:
        """``True`` if a live socket is currently held."""
        return self._conn.is_connected

    @property
    def socket_path(self) -> str:
        """The configured socket path."""
        return self._conn.socket_path

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"BirdClient(socket_path={self.socket_path!r}, "
            f"connected={self.is_connected}, version={self.server_version!r})"
        )

    # Allow `getattr(client, 'some_attr')` to fall through to the connection
    # for advanced use (e.g. setting timeout after construction).
    def __getattr__(self, name: str) -> Any:
        # Only called when normal attribute lookup fails.
        # Avoid infinite recursion by accessing _conn directly.
        return getattr(self.__dict__["_conn"], name)
