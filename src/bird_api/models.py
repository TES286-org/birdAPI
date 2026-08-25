"""Structured data models for parsed BIRD command responses.

Each model is a frozen dataclass carrying:

1. The parsed fields upper-layer code wants (typed, immutable).
2. The raw text of the daemon's reply as ``raw_text``.
3. The original wire command as ``command``.

A common :class:`ParsedResponse` base class carries (2) and (3); concrete
classes inherit from it and add their own typed fields. This way users
can always fall back to ``.raw_text`` if a parser's structured view does
not surface a particular field they need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

__all__ = [
    "ParsedResponse",
    "Status",
    "ProtocolRow",
    "ProtocolsResult",
    "ChannelState",
    "ProtocolDetail",
    "ProtocolsAllResult",
    "RouteEntry",
    "RouteResult",
    "MemoryRow",
    "MemoryInfo",
    "Symbol",
    "SymbolsResult",
    "InterfaceAddress",
    "InterfaceOne",
    "InterfacesResult",
    "InterfaceSummaryRow",
    "InterfacesSummaryResult",
    "EvalResult",
    "ConfigResult",
    "ActionResult",
    "BgpSession",
    "BgpSessionsResult",
    "StaticRoute",
    "StaticResult",
    "OspfArea",
    "OspfInfo",
    "OspfNeighborRow",
    "OspfNeighborsResult",
    "OspfInterface",
    "OspfInterfacesResult",
    "OspfStateResult",
    "OspfLsaRow",
    "OspfLsadbScope",
    "OspfLsadbResult",
    "BfdSessionRow",
    "BfdSessionDetail",
    "BfdSessionsResult",
    "RipInterfaceRow",
    "RipInterfacesResult",
    "RipNeighborRow",
    "RipNeighborsResult",
    "BabelInterfaceRow",
    "BabelInterfacesResult",
    "BabelNeighborRow",
    "BabelNeighborsResult",
    "MplsRangeRow",
    "MplsDomainRanges",
    "MplsRangesResult",
]


class ParsedResponse:
    """Marker base class for every parsed command response.

    Carries no fields itself — concrete subclasses are frozen dataclasses
    that declare ``raw_text: str`` and ``command: str`` as their first two
    fields, in that order, plus their own typed fields after.

    The two shared fields are surfaced here as type annotations only; the
    actual storage comes from the dataclass slots declared by each subclass.
    """

    raw_text: str
    command: str

    __slots__: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# show status
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Status(ParsedResponse):
    """Decoded ``show status`` response (see scripts/bird-protocol-research.md §4.1).

    The ``reconfig_state`` is the textual state from the final line:
    ``"up"`` for the common ``"Daemon is up and running"``,
    ``"shutdown"`` for ``"Shutdown in progress"``,
    ``"reconfig"`` for ``"Reconfiguration in progress"``.
    """

    raw_text: str
    command: str
    bird_version: str
    router_id: str
    hostname: str
    current_server_time: datetime | None
    last_reboot: datetime | None
    last_reconfiguration: datetime | None
    reconfig_state: str
    config_undo_in: int | None


# ---------------------------------------------------------------------------
# show protocols / show protocols all
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProtocolRow:
    """A single row from ``show protocols`` (or the summary row of ``show protocols all``)."""

    name: str
    protocol: str  # e.g. "BGP", "OSPF", "Direct", "Kernel"
    table: str  # "---" if unbound
    state: str  # "up" / "down" / "start" / etc.
    since: str  # BIRD's own formatted timestamp (raw)
    info: str  # freeform info column


@dataclass(frozen=True, slots=True)
class ProtocolsResult(ParsedResponse):
    """Decoded ``show protocols`` response."""

    raw_text: str
    command: str
    rows: list[ProtocolRow]


@dataclass(frozen=True, slots=True)
class ChannelState:
    """One ``Channel`` block under a verbose ``show protocols all`` entry."""

    name: str
    state: str  # "UP" / "DOWN" / etc.
    table: str
    preference: str
    input_filter: str
    output_filter: str
    routes_imported: int | None
    routes_filtered: int | None
    routes_exported: int | None
    routes_preferred: int | None
    route_change_stats: dict[str, dict[str, str]] = field(default_factory=dict)
    extra_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProtocolDetail:
    """Verbose block for a single protocol from ``show protocols all``."""

    name: str
    protocol: str
    table: str
    state: str
    since: str
    info: str
    created: str | None  # BIRD 3.x only
    description: str | None
    message: str | None
    router_id: str | None
    vrf: str | None
    channels: list[ChannelState]
    detail_lines: list[str]  # protocol-specific show_proto_info output (unparsed)


@dataclass(frozen=True, slots=True)
class ProtocolsAllResult(ParsedResponse):
    """Decoded ``show protocols all`` response."""

    raw_text: str
    command: str
    rows: list[ProtocolRow]  # the summary section (one per protocol)
    details: list[ProtocolDetail]  # the verbose blocks (one per protocol)


# ---------------------------------------------------------------------------
# show route family
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteEntry:
    """One route entry parsed from ``show route`` output.

    A "route entry" is one prefix row plus any ``via``/``dev`` continuation
    lines that belong to it (nexthops). For ``show route all`` the attribute
    block (code 1012) is captured in ``attribute_lines``.

    BIRD's route line is::

        <prefix> <dest> [<proto> <time>[ from <ip>]][ *|!][ (<pref>)[<extra>]]

    so non-primary routes carry no ``*``/``!`` marker at all, and BGP routes
    append protocol-specific info after the preference (e.g. ``(100) [i]``).
    """

    prefix: str
    destination: str  # BIRD's dest-name: "unicast", "blackhole", "unreachable", ...
    protocol: str
    timestamp: str
    primary: bool  # True if marked with '*' (best route in its network)
    via: list[str]  # each "via <gw> on <iface>" line
    dev: list[str]  # each "dev <iface>" line
    attribute_lines: list[str]  # code 1012 lines for ``show route all``
    raw_lines: list[str]  # original lines for round-trip inspection
    from_ip: str | None = None  # "from <ip>" inside the source brackets (BGP)
    sync_error: bool = False  # True if marked with '!' (kernel sync error)
    preference: int | None = None  # parsed from the trailing "(<n>)" info
    info: str = ""  # the raw info tail, e.g. "(100) [i]" for BGP routes

    @property
    def attributes(self) -> dict[str, str]:
        """The ``show route all`` attribute block as a ``key -> value`` dict.

        Lines without a ``: `` separator are treated as continuations of the
        previous attribute's value (BIRD wraps long lists such as large
        community sets). Values are kept as raw strings; typed views can be
        built on top by the caller.
        """
        result: dict[str, str] = {}
        last_key: str | None = None
        for line in self.attribute_lines:
            if ": " in line:
                key, _, value = line.partition(": ")
                key = key.strip()
                result[key] = value.strip()
                last_key = key
            elif line.endswith(":"):
                key = line[:-1].strip()
                result[key] = ""
                last_key = key
            elif last_key is not None:
                result[last_key] = (result[last_key] + " " + line.strip()).strip()
            # A leading line with no colon and no prior key is ignored
            # (defensive — never produced by BIRD).
        return result


@dataclass(frozen=True, slots=True)
class RouteResult(ParsedResponse):
    """Decoded ``show route`` / ``show route all`` response."""

    raw_text: str
    command: str
    table: str  # the table name from the "Table <name>:" header line
    entries: list[RouteEntry]
    stats: dict[str, str] = field(default_factory=dict)  # for `show route stats` (Total: ...)


# ---------------------------------------------------------------------------
# show memory
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryRow:
    """One row of ``show memory`` output, with both effective and overhead sizes."""

    label: str  # "Routing tables", "Route attributes", "Protocols", ...
    effective: str  # e.g. "73.7 kB"
    overhead: str  # e.g. "9.9 kB"


@dataclass(frozen=True, slots=True)
class MemoryInfo(ParsedResponse):
    """Decoded ``show memory`` response."""

    raw_text: str
    command: str
    rows: list[MemoryRow]
    page_stats: dict[str, str]  # {"Active pages": "5", "Kept free pages": "0", ...}


# ---------------------------------------------------------------------------
# show symbols
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Symbol:
    """One symbol entry from ``show symbols``."""

    name: str
    symbol_class: str  # "protocol", "routing table", "filter", "function", ...


@dataclass(frozen=True, slots=True)
class SymbolsResult(ParsedResponse):
    """Decoded ``show symbols`` response."""

    raw_text: str
    command: str
    symbols: list[Symbol]


# ---------------------------------------------------------------------------
# show interfaces / show interfaces summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InterfaceAddress:
    """One address line from ``show interfaces`` per-interface output."""

    address: str  # e.g. "127.0.0.1/8"
    flags: str  # e.g. "scope host" / "preferred"
    scope: str | None


@dataclass(frozen=True, slots=True)
class InterfaceOne:
    name: str
    admin_state: str  # "up" / "down"
    index: int | None
    master: str | None
    type_line: str  # raw line for type/flags/mtu
    addresses: list[InterfaceAddress]


@dataclass(frozen=True, slots=True)
class InterfacesResult(ParsedResponse):
    """Decoded per-interface block from ``show interfaces`` (NOT summary)."""

    raw_text: str
    command: str
    interfaces: list[InterfaceOne]


@dataclass(frozen=True, slots=True)
class InterfaceSummaryRow:
    name: str
    state: str  # "up" / "dn"
    ipv4: str  # "" if none
    ipv6: str  # "" if none


@dataclass(frozen=True, slots=True)
class InterfacesSummaryResult(ParsedResponse):
    """Decoded ``show interfaces summary`` response."""

    raw_text: str
    command: str
    rows: list[InterfaceSummaryRow]


# ---------------------------------------------------------------------------
# eval <expr>
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvalResult(ParsedResponse):
    """Decoded ``eval <expr>`` response."""

    raw_text: str
    command: str
    value: str


# ---------------------------------------------------------------------------
# configure ...  /  enable/disable/restart/reload / dump / etc.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfigResult(ParsedResponse):
    """Decoded ``configure ...`` family response.

    For ``configure check "<file>"`` ``Configuration OK`` is reflected in
    ``ok=True``; the library raises :class:`BirdCommandError` on errors,
    so reaching this class implies ``ok=True``.
    """

    raw_text: str
    command: str
    file: str | None
    ok: bool
    message: str  # e.g. "Configuration OK" / "Reconfigured"
    undo_scheduled_in: int | None  # for ``configure "<file>" timeout <sec>``


@dataclass(frozen=True, slots=True)
class ActionResult(ParsedResponse):
    """Generic response for ``enable`` / ``disable`` / ``restart`` / ``reload`` / ``dump`` commands.

    These commands emit a series of per-target continuation lines followed
    by a final ``0000`` (or an error code on failure). The structured view
    preserves those per-target messages verbatim.
    """

    raw_text: str
    command: str
    per_target: list[str]  # e.g. ["direct1: disabled", "kernel1: already disabled"]
    message: str  # final line text


# ---------------------------------------------------------------------------
# BGP sessions (parsed from `show protocols all` detail blocks)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BgpSession:
    """Structured view of one BGP protocol's ``show protocols all`` detail block.

    Fields mirror the lines emitted by ``bgp_show_proto_info()``
    (proto/bgp/bgp.c). Anything the parser does not model explicitly is
    preserved in ``extra_lines``.
    """

    name: str  # protocol name (e.g. "peer_b")
    state: str  # BGP state, e.g. "Established", "Idle", "Active"
    neighbor_address: str | None
    neighbor_port: int | None
    neighbor_as: int | None
    local_as: int | None
    neighbor_id: str | None
    session: str | None  # e.g. "internal multihop AS4"
    source_address: str | None
    hold_timer: str | None  # raw "175.586/240"
    keepalive_timer: str | None  # raw "63.888/80"
    send_hold_timer: str | None
    last_error: str | None
    capabilities: list[str]  # local + neighbor capability lines
    extra_lines: list[str]  # anything else, verbatim

    @property
    def established(self) -> bool:
        """``True`` when the session state is ``Established``."""
        return self.state == "Established"


@dataclass(frozen=True, slots=True)
class BgpSessionsResult(ParsedResponse):
    """Decoded BGP-session view of a ``show protocols all`` response."""

    raw_text: str
    command: str
    sessions: list[BgpSession]


# ---------------------------------------------------------------------------
# show static
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StaticRoute:
    """One route (with its nexthops) from ``show static``.

    ``dest_type`` is BIRD's route type for non-unicast routes
    (``blackhole``, ``unreachable``, ``prohibit``) or ``recursive`` for
    recursive nexthops; plain unicast routes carry ``unicast`` with their
    nexthops in ``nexthops``. Multi-path (ECMP) routes list every nexthop.
    """

    prefix: str
    dest_type: str
    nexthops: list[str]  # each "via <gw> on <iface> [onlink] [(bfd)]" or "dev <iface>" line
    dormant: bool  # True if any nexthop is marked "(dormant)"


@dataclass(frozen=True, slots=True)
class StaticResult(ParsedResponse):
    """Decoded ``show static`` response (routes of all matching protocols)."""

    raw_text: str
    command: str
    routes: list[StaticRoute]


# ---------------------------------------------------------------------------
# show ospf family
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OspfArea:
    """One area block from ``show ospf``."""

    area_id: str  # dotted form, e.g. "0.0.0.0"
    backbone: bool
    stub: bool | None
    nssa: bool | None
    transit: bool | None
    interfaces_count: int | None
    neighbors_count: int | None
    adjacent_count: int | None
    networks: list[str]  # "Area networks:" / "Area external networks:" lines
    extra_lines: list[str]


@dataclass(frozen=True, slots=True)
class OspfInfo(ParsedResponse):
    """Decoded ``show ospf`` response (per-protocol summary).

    When the protocol is not up the daemon answers a single
    ``<name>: is not up`` line; that is reflected by ``up=False`` and
    empty structured fields.
    """

    raw_text: str
    command: str
    protocol: str  # protocol name from the "<name>:" header
    up: bool
    rfc1583: bool | None
    stub_router: bool | None
    tick: int | None
    num_areas: int | None
    num_lsas: int | None
    areas: list[OspfArea]


@dataclass(frozen=True, slots=True)
class OspfNeighborRow:
    """One row of ``show ospf neighbors``."""

    router_id: str
    priority: int | None
    state: str  # OSPF state name, e.g. "Full"
    position: str  # "PtP", "DR", "BDR" or "Other"
    dead_timer: str
    interface: str
    router_ip: str


@dataclass(frozen=True, slots=True)
class OspfNeighborsResult(ParsedResponse):
    """Decoded ``show ospf neighbors`` response."""

    raw_text: str
    command: str
    protocol: str
    up: bool
    rows: list[OspfNeighborRow]


@dataclass(frozen=True, slots=True)
class OspfInterface:
    """One interface block from ``show ospf interface``."""

    name: str
    header: str  # full header line, e.g. "Interface eth0 (21.0.22.68/32)"
    fields: dict[str, str]  # every "Key: value" pair (Type, Area, State, Cost, ...)
    extra_lines: list[str]

    @property
    def type(self) -> str | None:
        return self.fields.get("Type")

    @property
    def area(self) -> str | None:
        return self.fields.get("Area")

    @property
    def state(self) -> str | None:
        return self.fields.get("State")

    @property
    def cost(self) -> int | None:
        value = self.fields.get("Cost")
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class OspfInterfacesResult(ParsedResponse):
    """Decoded ``show ospf interface`` response."""

    raw_text: str
    command: str
    protocol: str
    up: bool
    interfaces: list[OspfInterface]


@dataclass(frozen=True, slots=True)
class OspfStateResult(ParsedResponse):
    """Decoded ``show ospf state`` / ``show ospf topology`` response.

    The state dump is a deep protocol-specific tree (areas, routers,
    networks, links); this model preserves the full line list plus the
    area and router headers for indexing, and the raw text for display.
    """

    raw_text: str
    command: str
    protocol: str
    up: bool
    areas: list[str]  # "area <id>" header lines
    routers: list[str]  # "router <id>" header lines
    lines: list[str]  # every output line, in order


@dataclass(frozen=True, slots=True)
class OspfLsaRow:
    """One LSA row from ``show ospf lsadb``."""

    type: str  # hex type, e.g. "0001"
    ls_id: str
    router: str
    sequence: str
    age: int | None
    checksum: str


@dataclass(frozen=True, slots=True)
class OspfLsadbScope:
    """All LSAs under one scope header (Global / Area <id> / Link <iface>)."""

    scope: str
    rows: list[OspfLsaRow]


@dataclass(frozen=True, slots=True)
class OspfLsadbResult(ParsedResponse):
    """Decoded ``show ospf lsadb`` response."""

    raw_text: str
    command: str
    protocol: str
    up: bool
    scopes: list[OspfLsadbScope]

    @property
    def rows(self) -> list[OspfLsaRow]:
        """All LSA rows across scopes, in order."""
        return [row for scope in self.scopes for row in scope.rows]


# ---------------------------------------------------------------------------
# show bfd sessions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BfdSessionRow:
    """One row of the ``show bfd sessions`` table (non-verbose form)."""

    address: str
    interface: str  # "---" for multihop sessions
    state: str  # "Up", "Down", "Init", ...
    since: str  # timestamp of last state change
    interval: str
    timeout: str


@dataclass(frozen=True, slots=True)
class BfdSessionDetail:
    """One verbose session block from ``show bfd sessions all``."""

    address: str | None
    interface: str | None
    session_type: str | None  # "Direct" / "Multihop"
    state: str | None
    remote_state: str | None
    fields: dict[str, str]  # all "Key: value" pairs (incl. discriminators, timers)
    local_params: dict[str, str]
    remote_params: dict[str, str]
    extra_lines: list[str]


@dataclass(frozen=True, slots=True)
class BfdSessionsResult(ParsedResponse):
    """Decoded ``show bfd sessions`` response (table or verbose form)."""

    raw_text: str
    command: str
    protocols: list[str]  # protocol header names in output order
    up: bool  # False when the (single) protocol is not up
    rows: list[BfdSessionRow]  # non-verbose mode
    details: list[BfdSessionDetail]  # verbose mode ("all")

    @property
    def verbose(self) -> bool:
        """``True`` when the response carries verbose session blocks."""
        return bool(self.details) and not self.rows


# ---------------------------------------------------------------------------
# show rip interfaces / neighbors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RipInterfaceRow:
    """One row of ``show rip interfaces``."""

    interface: str
    state: str  # "Up" / "Down"
    metric: int | None
    neighbors: int | None
    timer: str


@dataclass(frozen=True, slots=True)
class RipInterfacesResult(ParsedResponse):
    """Decoded ``show rip interfaces`` response."""

    raw_text: str
    command: str
    protocol: str
    up: bool
    rows: list[RipInterfaceRow]


@dataclass(frozen=True, slots=True)
class RipNeighborRow:
    """One row of ``show rip neighbors``."""

    address: str
    interface: str
    metric: int | None
    routes: int | None
    seen: str


@dataclass(frozen=True, slots=True)
class RipNeighborsResult(ParsedResponse):
    """Decoded ``show rip neighbors`` response."""

    raw_text: str
    command: str
    protocol: str
    up: bool
    rows: list[RipNeighborRow]


# ---------------------------------------------------------------------------
# show babel interfaces / neighbors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BabelInterfaceRow:
    """One row of ``show babel interfaces``."""

    interface: str
    state: str  # "Up" / "Down"
    auth: str  # "No", "Yes" or "Perm"
    rxcost: int | None
    neighbors: int | None
    timer: str
    next_hop_v4: str
    next_hop_v6: str


@dataclass(frozen=True, slots=True)
class BabelInterfacesResult(ParsedResponse):
    """Decoded ``show babel interfaces`` response."""

    raw_text: str
    command: str
    protocol: str
    up: bool
    rows: list[BabelInterfaceRow]


@dataclass(frozen=True, slots=True)
class BabelNeighborRow:
    """One row of ``show babel neighbors``."""

    address: str
    interface: str
    metric: int | None
    routes: int | None
    hellos: int | None
    expires: str
    auth: str  # "Yes" / "No"
    rtt_ms: str


@dataclass(frozen=True, slots=True)
class BabelNeighborsResult(ParsedResponse):
    """Decoded ``show babel neighbors`` response."""

    raw_text: str
    command: str
    protocol: str
    up: bool
    rows: list[BabelNeighborRow]


# ---------------------------------------------------------------------------
# show mpls ranges
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MplsRangeRow:
    """One label-range row of ``show mpls ranges``."""

    name: str
    start: int | None
    length: int | None
    end: int | None
    labels: int | None  # number of labels currently allocated from the range
    last: int | None  # last allocated label (0 when none)


@dataclass(frozen=True, slots=True)
class MplsDomainRanges:
    """All ranges of one MPLS domain."""

    domain: str
    rows: list[MplsRangeRow]


@dataclass(frozen=True, slots=True)
class MplsRangesResult(ParsedResponse):
    """Decoded ``show mpls ranges`` response."""

    raw_text: str
    command: str
    domains: list[MplsDomainRanges]
