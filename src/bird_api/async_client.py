"""Asynchronous :class:`AsyncBirdClient` — the asyncio entry point.

Mirrors :class:`~bird_api.client.BirdClient` method-for-method with
``async`` signatures. Both clients share:

* the **same** wire-command builders (:mod:`bird_api.commands`) — byte
  identical commands, identical validation, identical injection safety;
* the same parsers and result models;
* the same client-side read-only allowlist.

so behaviour only differs in the execution model (blocking vs. awaited).
The parity is enforced by tests that replay the same call battery through
both stacks and compare the emitted command sequences.

Example
-------
    >>> import asyncio
    >>> from bird_api import AsyncBirdClient, RouteFilter, RouteQuery
    >>> async def main():
    ...     async with AsyncBirdClient(socket_path="/tmp/bird.ctl") as client:
    ...         status = await client.show_status()
    ...         print(status.router_id, status.bird_version)
    ...         q = RouteQuery().table("master4").where(RouteFilter.protocol("static_test"))
    ...         for entry in (await client.show_route_query(q)).entries:
    ...             print(entry.prefix, entry.preference)
    >>> asyncio.run(main())
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from . import commands as cmd
from .async_connection import AsyncBirdConnection, AsyncCallback
from .connection import DEFAULT_SOCKET_PATH, DEFAULT_TIMEOUT
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

__all__ = ["AsyncBirdClient", "DEFAULT_SOCKET_PATH", "DEFAULT_TIMEOUT"]


class AsyncBirdClient:
    """Asynchronous BIRD control-socket client (see :mod:`bird_api.async_connection`).

    Parameters are identical to :class:`~bird_api.client.BirdClient`;
    ``on_async`` additionally accepts coroutine functions.
    """

    def __init__(
        self,
        *,
        socket_path: str = DEFAULT_SOCKET_PATH,
        timeout: float = DEFAULT_TIMEOUT,
        on_async: AsyncCallback | None = None,
        read_only: bool = False,
    ) -> None:
        self._conn = AsyncBirdConnection(
            socket_path=socket_path,
            timeout=timeout,
            on_async=on_async,
            read_only=read_only,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> AsyncBirdClient:
        """Open the socket eagerly. Returns self for chaining."""
        await self._conn.connect()
        return self

    async def close(self) -> None:
        """Close the underlying socket. Safe to call multiple times."""
        await self._conn.close()

    async def __aenter__(self) -> AsyncBirdClient:
        await self._conn.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Read-only ``show`` commands
    # ------------------------------------------------------------------

    async def show_status(self) -> Status:
        """``show status`` — daemon version, router ID, uptime, state."""
        return parse_status(await self._conn.execute(cmd.build_show_status()))

    async def show_memory(self) -> MemoryInfo:
        """``show memory`` — memory-usage table per resource kind."""
        return parse_memory(await self._conn.execute(cmd.build_show_memory()))

    async def show_protocols(self, *, pattern: str | None = None) -> ProtocolsResult:
        """``show protocols [<pattern>]`` — one-line summary per protocol.

        A non-matching pattern yields an empty result (the daemon's
        ``8003 No protocols match`` is translated).
        """
        wire = cmd.build_show_protocols(pattern)
        try:
            result = await self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8003:
                return ProtocolsResult(raw_text=exc.message or "", command=wire, rows=[])
            raise
        return parse_protocols(result)

    async def show_protocols_all(self, *, pattern: str | None = None) -> ProtocolsAllResult:
        """``show protocols all [<pattern>]`` — full per-protocol verbose block."""
        wire = cmd.build_show_protocols_all(pattern)
        try:
            result = await self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8003:
                return ProtocolsAllResult(raw_text=exc.message or "", command=wire, rows=[], details=[])
            raise
        return parse_protocols_all(result)

    async def get_protocol(self, name: str) -> ProtocolDetail | None:
        """Fetch one protocol's verbose detail block by exact name (``None`` if absent)."""
        wire = cmd.build_get_protocol(name)
        try:
            result = await self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code in (8003, 9001):
                return None
            raise
        parsed = parse_protocols_all(result)
        for detail in parsed.details:
            if detail.name == name:
                return detail
        return None

    async def show_route(self, *, all: bool = False) -> RouteResult:
        """``show route [all]`` — all routes from the default table."""
        return parse_route(await self._conn.execute(cmd.build_show_route(all=all)))

    async def show_route_query(self, query: RouteQuery) -> RouteResult:
        """Execute a :class:`~bird_api.queries.RouteQuery` (full ``show route`` grammar)."""
        return parse_route(await self._conn.execute(cmd.build_show_route_query(query)))

    async def show_route_prefix(self, prefix: str, *, all: bool = False) -> RouteResult:
        """``show route <prefix> [all]`` — routes for a specific network."""
        return parse_route(await self._conn.execute(cmd.build_show_route_prefix(prefix, all=all)))

    async def show_route_for(self, address: str, *, all: bool = False) -> RouteResult:
        """``show route for <ip> [all]`` — routes used to reach <address>."""
        return parse_route(await self._conn.execute(cmd.build_show_route_for(address, all=all)))

    async def show_route_in(self, prefix: str, *, all: bool = False) -> RouteResult:
        """``show route in <prefix> [all]`` — routes contained within <prefix>."""
        return parse_route(await self._conn.execute(cmd.build_show_route_in(prefix, all=all)))

    async def show_route_table(self, table: str, *, all: bool = False) -> RouteResult:
        """``show route table <name> [all]`` — routes from a named table."""
        return parse_route(await self._conn.execute(cmd.build_show_route_table(table, all=all)))

    async def show_route_protocol(self, protocol: str, *, all: bool = False) -> RouteResult:
        """``show route protocol <name> [all]`` — routes from a specific protocol."""
        return parse_route(await self._conn.execute(cmd.build_show_route_protocol(protocol, all=all)))

    async def show_route_stats(self) -> RouteResult:
        """``show route stats`` — per-table and total route counts."""
        return parse_route(await self._conn.execute(cmd.build_show_route_stats()))

    async def show_symbols(self, *, table: bool = False) -> SymbolsResult:
        """``show symbols [table]`` — all symbols (or only routing tables)."""
        return parse_symbols(await self._conn.execute(cmd.build_show_symbols(table=table)))

    async def show_interfaces(self) -> InterfacesResult:
        """``show interfaces`` — full per-interface block."""
        return parse_interfaces(await self._conn.execute("show interfaces"))

    async def show_interfaces_summary(self) -> InterfacesSummaryResult:
        """``show interfaces summary`` — one-line summary per interface."""
        return parse_interfaces_summary(await self._conn.execute("show interfaces summary"))

    async def eval(self, expr: str) -> EvalResult:
        """``eval <expr>`` — evaluate a BIRD filter expression (single line)."""
        return parse_eval(await self._conn.execute(cmd.build_eval(expr)))

    # ------------------------------------------------------------------
    # High-level complex-query wrappers
    # ------------------------------------------------------------------

    async def route_lookup(
        self,
        address: str,
        *,
        table: str | None = None,
        all: bool = False,
    ) -> RouteResult:
        """Best-route lookup; a miss returns an empty result instead of raising."""
        wire = cmd.build_route_lookup(address, table=table, all=all)
        try:
            result = await self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8001:
                return RouteResult(
                    raw_text=exc.message or "Network not found",
                    command=exc.command or wire,
                    table=table or "",
                    entries=[],
                    stats={},
                )
            raise
        return parse_route(result)

    async def route_exists(self, prefix: str, *, table: str | None = None) -> bool:
        """``True`` if the exact prefix exists in the routing table."""
        wire = cmd.build_route_exists(prefix, table=table)
        try:
            await self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8001:
                return False
            raise
        return True

    async def count_routes(self, *, table: str | None = None) -> int:
        """Number of routes in one table (or the total across all tables)."""
        result = parse_route(await self._conn.execute(cmd.build_count_routes(table=table)))
        if table is not None:
            value = result.stats.get(f"{table}.routes")
            if value is None:
                raise ValueError(  # noqa: TRY003 - simple guard, message is short
                    f"no route count reported for table {table!r}"
                )
            return int(value)
        value = result.stats.get("routes")
        if value is None:
            return 0
        return int(value)

    async def bgp_sessions(self, *, pattern: str | None = None) -> BgpSessionsResult:
        """Structured BGP session overview — ``show protocols all [<pattern>]``."""
        wire = cmd.build_bgp_sessions(pattern)
        try:
            result = await self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8003:
                return BgpSessionsResult(raw_text=exc.message or "", command=wire, sessions=[])
            raise
        parsed = parse_protocols_all(result)
        return parse_bgp_sessions(parsed.details, raw_text=parsed.raw_text, command=wire)

    async def find_protocols(self, pattern: str) -> ProtocolsResult:
        """``show protocols <pattern>`` — shell-like pattern matching."""
        wire = cmd.build_find_protocols(pattern)
        try:
            result = await self._conn.execute(wire)
        except BirdCommandError as exc:
            if exc.code == 8003:
                return ProtocolsResult(raw_text=exc.message or "", command=wire, rows=[])
            raise
        return parse_protocols(result)

    # ------------------------------------------------------------------
    # Configuration commands
    # ------------------------------------------------------------------

    async def configure(
        self,
        file: str,
        *,
        timeout: int | None = None,
        soft: bool = False,
        check: bool = False,
    ) -> ConfigResult:
        """Apply (or check) a BIRD configuration file."""
        wire = cmd.build_configure(file, timeout=timeout, soft=soft, check=check)
        return parse_configure(await self._conn.execute(wire))

    async def configure_status(self) -> ConfigResult:
        """``configure status`` — current reconfiguration state."""
        return parse_configure(await self._conn.execute("configure status"))

    async def configure_confirm(self) -> ConfigResult:
        """``configure confirm`` — confirm a pending reconfiguration."""
        return parse_configure(await self._conn.execute("configure confirm"))

    async def configure_undo(self) -> ConfigResult:
        """``configure undo`` — revert to the previous configuration."""
        return parse_configure(await self._conn.execute("configure undo"))

    # ------------------------------------------------------------------
    # Per-protocol actions
    # ------------------------------------------------------------------

    async def enable(self, pattern: str = "all") -> ActionResult:
        """``enable <pattern>`` — enable matching protocols."""
        return parse_action(await self._conn.execute(cmd.build_enable(pattern)))

    async def disable(self, pattern: str = "all") -> ActionResult:
        """``disable <pattern>`` — disable matching protocols."""
        return parse_action(await self._conn.execute(cmd.build_disable(pattern)))

    async def restart(self, pattern: str = "all") -> ActionResult:
        """``restart <pattern>`` — restart matching protocols."""
        return parse_action(await self._conn.execute(cmd.build_restart(pattern)))

    async def reload(self, pattern: str = "all", *, direction: str = "both") -> ActionResult:
        """``reload [in|out] <pattern>`` — reload routes for matching protocols."""
        return parse_action(await self._conn.execute(cmd.build_reload(pattern, direction=direction)))

    async def dump(self, kind: str, file: str) -> ActionResult:
        """``dump <kind> "<file>"`` — write a runtime dump to <file>."""
        return parse_action(await self._conn.execute(cmd.build_dump(kind, file)))

    # ------------------------------------------------------------------
    # Protocol-specific show commands
    # ------------------------------------------------------------------

    async def show_static(self, pattern: str | None = None) -> StaticResult:
        """``show static [<name>]`` — routes of one (or the only) static protocol."""
        return parse_static(await self._conn.execute(cmd.build_show_static(pattern)))

    async def show_ospf(self, pattern: str | None = None) -> OspfInfo:
        """``show ospf [<name>]`` — per-protocol area summary."""
        return parse_ospf(await self._conn.execute(cmd.build_show_ospf(pattern)))

    async def show_ospf_neighbors(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> OspfNeighborsResult:
        """``show ospf neighbors [<name>] ["<interface>"]`` — neighbor table."""
        wire = cmd.build_show_ospf_neighbors(pattern, interface)
        return parse_ospf_neighbors(await self._conn.execute(wire))

    async def show_ospf_interface(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> OspfInterfacesResult:
        """``show ospf interface [<name>] ["<interface>"]`` — interface details."""
        wire = cmd.build_show_ospf_interface(pattern, interface)
        return parse_ospf_interfaces(await self._conn.execute(wire))

    async def show_ospf_state(self, pattern: str | None = None, *, all: bool = False) -> OspfStateResult:
        """``show ospf state [<name>] [all]`` — reachable network topology dump."""
        return parse_ospf_state(await self._conn.execute(cmd.build_show_ospf_state(pattern, all=all)))

    async def show_ospf_topology(self, pattern: str | None = None, *, all: bool = False) -> OspfStateResult:
        """``show ospf topology [<name>] [all]`` — same shape as :meth:`show_ospf_state`."""
        return parse_ospf_state(await self._conn.execute(cmd.build_show_ospf_topology(pattern, all=all)))

    async def show_ospf_lsadb(
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
        """``show ospf lsadb`` — the link-state database (with filters)."""
        wire = cmd.build_show_ospf_lsadb(
            pattern,
            scope=scope,
            area=area,
            lsa_type=lsa_type,
            lsid=lsid,
            router=router,
            self_originated=self_originated,
        )
        return parse_ospf_lsadb(await self._conn.execute(wire))

    async def show_bfd_sessions(
        self,
        pattern: str | None = None,
        *,
        address: str | None = None,
        interface: str | None = None,
        family: str | None = None,
        mode: str | None = None,
        all: bool = False,
    ) -> BfdSessionsResult:
        """``show bfd sessions`` — BFD session table (or verbose blocks with ``all``)."""
        wire = cmd.build_show_bfd_sessions(
            pattern,
            address=address,
            interface=interface,
            family=family,
            mode=mode,
            all=all,
        )
        return parse_bfd_sessions(await self._conn.execute(wire))

    async def show_rip_interfaces(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> RipInterfacesResult:
        """``show rip interfaces [<name>] ["<interface>"]`` — RIP interface table."""
        wire = cmd.build_show_rip_interfaces(pattern, interface)
        return parse_rip_interfaces(await self._conn.execute(wire))

    async def show_rip_neighbors(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> RipNeighborsResult:
        """``show rip neighbors [<name>] ["<interface>"]`` — RIP neighbor table."""
        wire = cmd.build_show_rip_neighbors(pattern, interface)
        return parse_rip_neighbors(await self._conn.execute(wire))

    async def show_babel_interfaces(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> BabelInterfacesResult:
        """``show babel interfaces [<name>] ["<interface>"]`` — Babel interface table."""
        wire = cmd.build_show_babel_interfaces(pattern, interface)
        return parse_babel_interfaces(await self._conn.execute(wire))

    async def show_babel_neighbors(
        self,
        pattern: str | None = None,
        interface: str | None = None,
    ) -> BabelNeighborsResult:
        """``show babel neighbors [<name>] ["<interface>"]`` — Babel neighbor table."""
        wire = cmd.build_show_babel_neighbors(pattern, interface)
        return parse_babel_neighbors(await self._conn.execute(wire))

    async def show_mpls_ranges(self) -> MplsRangesResult:
        """``show mpls ranges`` — label ranges of every MPLS domain."""
        return parse_mpls_ranges(await self._conn.execute(cmd.build_show_mpls_ranges()))

    # ------------------------------------------------------------------
    # Session control
    # ------------------------------------------------------------------

    async def restrict(self) -> ActionResult:
        """``restrict`` — mark the current session as read-only (server-side)."""
        return parse_action(await self._conn.execute("restrict"))

    async def down(self) -> ActionResult:
        """``down`` — request a clean daemon shutdown."""
        return parse_action(await self._conn.execute("down"))

    async def graceful_restart(self) -> ActionResult:
        """``graceful restart`` — request a graceful restart."""
        return parse_action(await self._conn.execute("graceful restart"))

    # ------------------------------------------------------------------
    # Escape hatch
    # ------------------------------------------------------------------

    async def raw(self, command: str) -> CommandResult:
        """Run an arbitrary single-line BIRD command (subject to read-only checks)."""
        return await self._conn.execute(command)

    async def raw_lines(self, command: str) -> AsyncIterator[str]:
        """Like :meth:`raw` but yields only the per-line text."""
        result = await self._conn.execute(command)
        for line in result.raw_lines:
            yield line

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def server_version(self) -> str | None:
        """BIRD version reported in the hello banner (``None`` if not connected)."""
        return self._conn.server_version

    @property
    def is_connected(self) -> bool:
        """``True`` if a live stream is currently held."""
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
            f"AsyncBirdClient(socket_path={self.socket_path!r}, "
            f"connected={self.is_connected}, version={self.server_version!r})"
        )
