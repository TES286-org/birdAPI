"""High-level :class:`BirdClient` — the primary entry point for upper-layer code.

A :class:`BirdClient` owns a :class:`~bird_api.connection.BirdConnection`
(persistent Unix socket) and exposes one method per supported BIRD command
family. Each method:

1. **Validates** every caller-supplied argument by building the wire
   command through :mod:`bird_api.commands` (backed by
   :mod:`bird_api.validation` — the injection-prevention layer; see that
   module's docstring for the threat model).
2. Calls :meth:`BirdConnection.execute` to send it and collect the response.
3. Dispatches the response to the matching parser, which returns a typed
   :class:`~bird_api.models.ParsedResponse` carrying both structured fields
   and the original raw text.

The connection is opened lazily on the first command and reused across
all subsequent commands until :meth:`close` is called. If the underlying
socket dies (e.g. the daemon was restarted), the next command triggers a
transparent reconnect. An asyncio counterpart with an identical API lives
in :mod:`bird_api.async_client` — both share the same command builders,
so their wire behaviour is byte-identical by construction.

Complex queries
---------------
For everything the one-shot methods cannot express, use
:class:`~bird_api.queries.RouteQuery` (the full ``show route`` grammar) and
:class:`~bird_api.queries.RouteFilter` (a validated filter algebra), or the
higher-level convenience wrappers (:meth:`route_lookup`, :meth:`route_exists`,
:meth:`count_routes`, :meth:`bgp_sessions`, :meth:`get_protocol`).

Example
-------
    >>> from bird_api import BirdClient, RouteFilter, RouteQuery
    >>> with BirdClient(socket_path="/tmp/bird.ctl") as client:
    ...     status = client.show_status()
    ...     print(status.router_id, status.bird_version)
    ...     for row in client.show_protocols(pattern="static*").rows:
    ...         print(row.name, row.state)
    ...     q = RouteQuery().table("master4").where(RouteFilter.protocol("static_test"))
    ...     for entry in client.show_route_query(q).entries:
    ...         print(entry.prefix, entry.preference)
"""

from __future__ import annotations

from collections.abc import Iterator

from . import commands as cmd
from .connection import DEFAULT_SOCKET_PATH, DEFAULT_TIMEOUT, AsyncCallback, BirdConnection
from .exceptions import BirdCommandError
from .models import (
    ActionResult,
    BabelInterfacesResult,
    BabelNeighborsResult,
    BfdSessionsResult,
    BgpSessionsResult,
    ConfigResult,
    EvalResult,
    InterfacesResult,
    InterfacesSummaryResult,
    MemoryInfo,
    MplsRangesResult,
    OspfInfo,
    OspfInterfacesResult,
    OspfLsadbResult,
    OspfNeighborsResult,
    OspfStateResult,
    ProtocolDetail,
    ProtocolsAllResult,
    ProtocolsResult,
    RipInterfacesResult,
    RipNeighborsResult,
    RouteResult,
    StaticResult,
    Status,
    SymbolsResult,
)
from .parsers import (
    parse_action,
    parse_babel_interfaces,
    parse_babel_neighbors,
    parse_bfd_sessions,
    parse_bgp_sessions,
    parse_configure,
    parse_eval,
    parse_interfaces,
    parse_interfaces_summary,
    parse_memory,
    parse_mpls_ranges,
    parse_ospf,
    parse_ospf_interfaces,
    parse_ospf_lsadb,
    parse_ospf_neighbors,
    parse_ospf_state,
    parse_protocols,
    parse_protocols_all,
    parse_rip_interfaces,
    parse_rip_neighbors,
    parse_route,
    parse_static,
    parse_status,
    parse_symbols,
)
from .protocol import CommandResult
from .queries import RouteQuery

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
    read_only:
        When ``True``, the client refuses to send any command that mutates
        daemon state (``configure``, ``enable``, ``disable``, ``restart``,
        ``reload``, ``dump``, ``down``, …) — including via :meth:`raw`.
        The check happens client-side, before anything is written to the
        socket, and survives transparent reconnects (unlike the server-side
        ``restrict`` command, which is per-session). Combine both for
        defence in depth.
    """

    def __init__(
        self,
        *,
        socket_path: str = DEFAULT_SOCKET_PATH,
        timeout: float = DEFAULT_TIMEOUT,
        on_async: AsyncCallback | None = None,
        read_only: bool = False,
    ) -> None:
        self._conn = BirdConnection(
            socket_path=socket_path,
            timeout=timeout,
            on_async=on_async,
            read_only=read_only,
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
        return parse_status(self._conn.execute(cmd.build_show_status()))

    def show_memory(self) -> MemoryInfo:
        """``show memory`` — memory-usage table per resource kind."""
        return parse_memory(self._conn.execute(cmd.build_show_memory()))

    def show_protocols(self, *, pattern: str | None = None) -> ProtocolsResult:
        """``show protocols [<pattern>]`` — one-line summary per protocol.

        Parameters
        ----------
        pattern:
            Optional protocol name or shell-like pattern (``bgp*``, ``ospf?``).
            Bare identifiers and ``"all"`` are sent unquoted; anything else
            is double-quoted, which is how BIRD's grammar accepts patterns.
            A non-matching pattern yields an empty result (the daemon's
            ``8003 No protocols match`` is translated).
        """
        wire = cmd.build_show_protocols(pattern)
        try:
            result = self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8003:
                return ProtocolsResult(raw_text=exc.message or "", command=wire, rows=[])
            raise
        return parse_protocols(result)

    def show_protocols_all(self, *, pattern: str | None = None) -> ProtocolsAllResult:
        """``show protocols all [<pattern>]`` — full per-protocol verbose block."""
        wire = cmd.build_show_protocols_all(pattern)
        try:
            result = self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8003:
                return ProtocolsAllResult(raw_text=exc.message or "", command=wire, rows=[], details=[])
            raise
        return parse_protocols_all(result)

    def get_protocol(self, name: str) -> ProtocolDetail | None:
        """Fetch one protocol's verbose detail block by exact name.

        Returns ``None`` when no protocol with that name exists (the daemon
        answers a syntax error for unknown symbols, which we translate into
        a clean miss). Prefer :meth:`show_protocols_all` with a pattern for
        fuzzy matching.
        """
        wire = cmd.build_get_protocol(name)
        try:
            result = self._conn.execute(wire)
        except BirdCommandError as exc:
            # Unknown symbol is reported as a parse error (9001) by BIRD, and
            # a non-matching pattern as 8003; treat both as "not found".
            if exc.code in (8003, 9001):
                return None
            raise
        parsed = parse_protocols_all(result)
        for detail in parsed.details:
            if detail.name == name:
                return detail
        return None

    def show_route(self, *, all: bool = False) -> RouteResult:
        """``show route [all]`` — all routes from the default table.

        Pass ``all=True`` for the verbose attribute dump.
        """
        return parse_route(self._conn.execute(cmd.build_show_route(all=all)))

    def show_route_query(self, query: RouteQuery) -> RouteResult:
        """Execute a :class:`~bird_api.queries.RouteQuery` (full ``show route`` grammar).

        The query is validated at build time by the builder itself; this
        method merely assembles, sends, and parses::

            client.show_route_query(
                RouteQuery().table("master4").protocol("bgp1").all_details()
            )
        """
        return parse_route(self._conn.execute(cmd.build_show_route_query(query)))

    def show_route_prefix(self, prefix: str, *, all: bool = False) -> RouteResult:
        """``show route <prefix> [all]`` — routes for a specific network."""
        return parse_route(self._conn.execute(cmd.build_show_route_prefix(prefix, all=all)))

    def show_route_for(self, address: str, *, all: bool = False) -> RouteResult:
        """``show route for <ip> [all]`` — routes that would be used to reach <address>."""
        return parse_route(self._conn.execute(cmd.build_show_route_for(address, all=all)))

    def show_route_in(self, prefix: str, *, all: bool = False) -> RouteResult:
        """``show route in <prefix> [all]`` — routes contained within <prefix>."""
        return parse_route(self._conn.execute(cmd.build_show_route_in(prefix, all=all)))

    def show_route_table(self, table: str, *, all: bool = False) -> RouteResult:
        """``show route table <name> [all]`` — routes from a named table."""
        return parse_route(self._conn.execute(cmd.build_show_route_table(table, all=all)))

    def show_route_protocol(self, protocol: str, *, all: bool = False) -> RouteResult:
        """``show route protocol <name> [all]`` — routes learned from a specific protocol."""
        return parse_route(self._conn.execute(cmd.build_show_route_protocol(protocol, all=all)))

    def show_route_stats(self) -> RouteResult:
        """``show route stats`` — per-table and total route counts."""
        return parse_route(self._conn.execute(cmd.build_show_route_stats()))

    def show_symbols(self, *, table: bool = False) -> SymbolsResult:
        """``show symbols [table]`` — all symbols (or only routing tables)."""
        return parse_symbols(self._conn.execute(cmd.build_show_symbols(table=table)))

    def show_interfaces(self) -> InterfacesResult:
        """``show interfaces`` — full per-interface block."""
        return parse_interfaces(self._conn.execute("show interfaces"))

    def show_interfaces_summary(self) -> InterfacesSummaryResult:
        """``show interfaces summary`` — one-line summary per interface."""
        return parse_interfaces_summary(self._conn.execute("show interfaces summary"))

    def eval(self, expr: str) -> EvalResult:
        """``eval <expr>`` — evaluate a BIRD filter expression.

        Use ``eval("1+2")`` to compute arithmetic; pass a quoted filter
        expression to evaluate complex filters. The daemon's response
        text becomes ``EvalResult.value``.

        The expression is validated to be a single line of printable
        ASCII — this preserves wire framing (no command injection) while
        allowing the full filter language.
        """
        return parse_eval(self._conn.execute(cmd.build_eval(expr)))

    # ------------------------------------------------------------------
    # High-level complex-query wrappers
    # ------------------------------------------------------------------

    def route_lookup(
        self,
        address: str,
        *,
        table: str | None = None,
        all: bool = False,
    ) -> RouteResult:
        """Best-route lookup for one address — ``show route for <ip>``.

        Unlike :meth:`show_route_for`, a lookup miss is not an error: BIRD's
        ``8001 Network not found`` is translated into an empty
        :class:`~bird_api.models.RouteResult` (``entries == []``), because
        "no route" is a legitimate answer to a lookup. The raw text always
        reflects exactly what the daemon sent.
        """
        wire = cmd.build_route_lookup(address, table=table, all=all)
        try:
            result = self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8001:  # "Network not found"
                return RouteResult(
                    raw_text=exc.message or "Network not found",
                    command=exc.command or wire,
                    table=table or "",
                    entries=[],
                    stats={},
                )
            raise
        return parse_route(result)

    def route_exists(self, prefix: str, *, table: str | None = None) -> bool:
        """``True`` if the exact prefix exists in the routing table."""
        wire = cmd.build_route_exists(prefix, table=table)
        try:
            self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8001:
                return False
            raise
        return True

    def count_routes(self, *, table: str | None = None) -> int:
        """Number of routes in one table (or the grand total across all tables).

        Uses ``show route count`` (no route lines are transferred — the
        daemon only reports the counters, which matters on tables with
        millions of routes).
        """
        result = parse_route(self._conn.execute(cmd.build_count_routes(table=table)))
        if table is not None:
            value = result.stats.get(f"{table}.routes")
            if value is None:
                raise ValueError(  # noqa: TRY003 - simple guard, message is short
                    f"no route count reported for table {table!r}"
                )
            return int(value)
        value = result.stats.get("routes")
        if value is None:
            # No "Total:" line means zero routes everywhere.
            return 0
        return int(value)

    def bgp_sessions(self, *, pattern: str | None = None) -> BgpSessionsResult:
        """Structured BGP session overview — ``show protocols all [<pattern>]``.

        Filters the protocol list to BGP and decodes each session's detail
        block (state, neighbor address/AS/ID, timers, session type, …).
        Pass ``pattern="peer_*"`` to restrict the set.
        """
        wire = cmd.build_bgp_sessions(pattern)
        try:
            result = self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8003:
                return BgpSessionsResult(raw_text=exc.message or "", command=wire, sessions=[])
            raise
        parsed = parse_protocols_all(result)
        return parse_bgp_sessions(parsed.details, raw_text=parsed.raw_text, command=wire)

    def find_protocols(self, pattern: str) -> ProtocolsResult:
        """``show protocols <pattern>`` — shell-like pattern matching.

        Example patterns: ``"bgp*"`` (all BGP), ``"ospf?"``, ``"static*"``.
        """
        wire = cmd.build_find_protocols(pattern)
        try:
            result = self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8003:
                return ProtocolsResult(raw_text=exc.message or "", command=wire, rows=[])
            raise
        return parse_protocols(result)

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
            this with double quotes per BIRD's lexer rules; the path must
            therefore be printable ASCII without double quotes.
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
        wire = cmd.build_configure(file, timeout=timeout, soft=soft, check=check)
        return parse_configure(self._conn.execute(wire))

    def configure_status(self) -> ConfigResult:
        """``configure status`` — current reconfiguration state."""
        return parse_configure(self._conn.execute("configure status"))

    def configure_confirm(self) -> ConfigResult:
        """``configure confirm`` — confirm a pending reconfiguration."""
        return parse_configure(self._conn.execute("configure confirm"))

    def configure_undo(self) -> ConfigResult:
        """``configure undo`` — revert to the previous configuration."""
        return parse_configure(self._conn.execute("configure undo"))

    # ------------------------------------------------------------------
    # Per-protocol actions
    # ------------------------------------------------------------------

    def enable(self, pattern: str = "all") -> ActionResult:
        """``enable <pattern>`` — enable matching protocols."""
        return parse_action(self._conn.execute(cmd.build_enable(pattern)))

    def disable(self, pattern: str = "all") -> ActionResult:
        """``disable <pattern>`` — disable matching protocols."""
        return parse_action(self._conn.execute(cmd.build_disable(pattern)))

    def restart(self, pattern: str = "all") -> ActionResult:
        """``restart <pattern>`` — restart matching protocols."""
        return parse_action(self._conn.execute(cmd.build_restart(pattern)))

    def reload(self, pattern: str = "all", *, direction: str = "both") -> ActionResult:
        """``reload [in|out] <pattern>`` — reload routes for matching protocols.

        BIRD 3.x rejects ``reload <pattern>`` (it requires ``in`` or ``out``).
        The default ``direction="both"`` issues ``reload <pattern>`` which
        may fail on BIRD 3.x; use ``direction="in"`` or ``direction="out"``
        for compatibility with BIRD 3.x.
        """
        return parse_action(self._conn.execute(cmd.build_reload(pattern, direction=direction)))

    def dump(self, kind: str, file: str) -> ActionResult:
        """``dump <kind> "<file>"`` — write a runtime dump to <file>.

        ``kind`` must be one of: ``resources``, ``sockets``, ``ao keys``,
        ``events``, ``interfaces``, ``neighbors``, ``attributes``,
        ``routes``, ``protocols``, ``filter all``.
        """
        return parse_action(self._conn.execute(cmd.build_dump(kind, file)))

    # ------------------------------------------------------------------
    # Protocol-specific show commands
    # ------------------------------------------------------------------

    def show_static(self, pattern: str | None = None) -> StaticResult:
        """``show static [<name>]`` — routes of one (or the only) static protocol."""
        return parse_static(self._conn.execute(cmd.build_show_static(pattern)))

    def show_ospf(self, pattern: str | None = None) -> OspfInfo:
        """``show ospf [<name>]`` — per-protocol area summary.

        When several OSPF protocols run, the daemon requires an explicit
        name (``proto_get_named`` errors with "multiple protocols").
        """
        return parse_ospf(self._conn.execute(cmd.build_show_ospf(pattern)))

    def show_ospf_neighbors(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> OspfNeighborsResult:
        """``show ospf neighbors [<name>] ["<interface>"]`` — neighbor table."""
        wire = cmd.build_show_ospf_neighbors(pattern, interface)
        return parse_ospf_neighbors(self._conn.execute(wire))

    def show_ospf_interface(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> OspfInterfacesResult:
        """``show ospf interface [<name>] ["<interface>"]`` — interface details."""
        wire = cmd.build_show_ospf_interface(pattern, interface)
        return parse_ospf_interfaces(self._conn.execute(wire))

    def show_ospf_state(self, pattern: str | None = None, *, all: bool = False) -> OspfStateResult:
        """``show ospf state [<name>] [all]`` — reachable network topology dump."""
        return parse_ospf_state(self._conn.execute(cmd.build_show_ospf_state(pattern, all=all)))

    def show_ospf_topology(self, pattern: str | None = None, *, all: bool = False) -> OspfStateResult:
        """``show ospf topology [<name>] [all]`` — same shape as :meth:`show_ospf_state`."""
        return parse_ospf_state(self._conn.execute(cmd.build_show_ospf_topology(pattern, all=all)))

    def show_ospf_lsadb(
        self,
        pattern: str | None = None,
        *,
        scope: str = "area",
        area: str | None = None,
        lsa_type: int | None = None,
        lsid: str | None = None,
        router: str | None = None,
        self_originated: bool = False,
    ) -> OspfLsadbResult:
        """``show ospf lsadb`` — the link-state database.

        Filters mirror the daemon's grammar: ``scope`` (``"global"`` /
        ``"area"`` / ``"link"``), ``area`` (dotted area ID), ``lsa_type``,
        ``lsid`` / ``router`` (IPs), ``self_originated``.
        """
        wire = cmd.build_show_ospf_lsadb(
            pattern,
            scope=scope,
            area=area,
            lsa_type=lsa_type,
            lsid=lsid,
            router=router,
            self_originated=self_originated,
        )
        return parse_ospf_lsadb(self._conn.execute(wire))

    def show_bfd_sessions(
        self,
        pattern: str | None = None,
        *,
        address: str | None = None,
        interface: str | None = None,
        family: str | None = None,
        mode: str | None = None,
        all: bool = False,
    ) -> BfdSessionsResult:
        """``show bfd sessions`` — BFD session table.

        Filters: ``pattern`` (protocol name), ``address`` (IP or prefix),
        ``interface``, ``family`` (``"ipv4"`` / ``"ipv6"``), ``mode``
        (``"direct"`` / ``"multihop"``). ``all=True`` requests the verbose
        per-session blocks instead of the table.
        """
        wire = cmd.build_show_bfd_sessions(
            pattern,
            address=address,
            interface=interface,
            family=family,
            mode=mode,
            all=all,
        )
        return parse_bfd_sessions(self._conn.execute(wire))

    def show_rip_interfaces(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> RipInterfacesResult:
        """``show rip interfaces [<name>] ["<interface>"]`` — RIP interface table."""
        wire = cmd.build_show_rip_interfaces(pattern, interface)
        return parse_rip_interfaces(self._conn.execute(wire))

    def show_rip_neighbors(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> RipNeighborsResult:
        """``show rip neighbors [<name>] ["<interface>"]`` — RIP neighbor table."""
        wire = cmd.build_show_rip_neighbors(pattern, interface)
        return parse_rip_neighbors(self._conn.execute(wire))

    def show_babel_interfaces(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> BabelInterfacesResult:
        """``show babel interfaces [<name>] ["<interface>"]`` — Babel interface table."""
        wire = cmd.build_show_babel_interfaces(pattern, interface)
        return parse_babel_interfaces(self._conn.execute(wire))

    def show_babel_neighbors(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> BabelNeighborsResult:
        """``show babel neighbors [<name>] ["<interface>"]`` — Babel neighbor table."""
        wire = cmd.build_show_babel_neighbors(pattern, interface)
        return parse_babel_neighbors(self._conn.execute(wire))

    def show_mpls_ranges(self) -> MplsRangesResult:
        """``show mpls ranges`` — label ranges of every MPLS domain."""
        return parse_mpls_ranges(self._conn.execute(cmd.build_show_mpls_ranges()))

    # ------------------------------------------------------------------
    # Session control
    # ------------------------------------------------------------------

    def restrict(self) -> ActionResult:
        """``restrict`` — mark the current session as read-only.

        Subsequent privileged commands (configure, enable, disable,
        restart, dump, etc.) will fail with code 8007 (Access denied).
        Note: this is server-side and per-session — if the connection is
        re-established (e.g. after a daemon restart) the restriction is
        gone. Use ``BirdClient(read_only=True)`` for a reconnect-safe
        client-side allowlist.
        """
        # `restrict` always returns code 0016 (which is < 8000, so it is
        # treated as success by the connection layer).
        return parse_action(self._conn.execute("restrict"))

    def down(self) -> ActionResult:
        """``down`` — request a clean daemon shutdown.

        The daemon will exit shortly after this command; subsequent
        commands will fail with :class:`~bird_api.exceptions.BirdConnectionError`.
        """
        return parse_action(self._conn.execute("down"))

    def graceful_restart(self) -> ActionResult:
        """``graceful restart`` — request a graceful restart."""
        return parse_action(self._conn.execute("graceful restart"))

    # ------------------------------------------------------------------
    # Escape hatch — for commands not yet wrapped by a method.
    # ------------------------------------------------------------------

    def raw(self, command: str) -> CommandResult:
        """Run an arbitrary BIRD command and return the unstructured result.

        Use this for commands the wrapper doesn't yet expose; it preserves
        the full :class:`~bird_api.protocol.CommandResult` (with ``.sections``,
        ``.raw_text``, ``.async_messages``, ``.final_code``, ``.success``,
        ``.error_message``) for the caller to parse manually.

        Security: the command must be a single line without control
        characters (a newline would let a second command ride the same
        frame — the classic injection vector), and it is rejected entirely
        when the client was constructed with ``read_only=True``.

        Raises :class:`~bird_api.exceptions.BirdCommandError` if the daemon
        reports a runtime/parse error.
        """
        return self._conn.execute(command)

    def raw_lines(self, command: str) -> Iterator[str]:
        """Like :meth:`raw` but yields only the per-line text (no protocol prefix).

        Useful for ad-hoc streaming-style inspection of new commands.
        Subject to the same single-line / read-only restrictions as
        :meth:`raw`.
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

    @property
    def read_only(self) -> bool:
        """Whether this client enforces the read-only command allowlist."""
        return self._conn.read_only

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"BirdClient(socket_path={self.socket_path!r}, "
            f"connected={self.is_connected}, version={self.server_version!r})"
        )
