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
    """

    prefix: str
    destination: str  # BIRD's dest-name, often "---"
    protocol: str
    timestamp: str
    primary: bool  # True if marked with '*' (primary route); False if '!' (sync error)
    via: list[str]  # each "via <gw> on <iface>" line
    dev: list[str]  # each "dev <iface>" line
    attribute_lines: list[str]  # code 1012 lines for ``show route all``
    raw_lines: list[str]  # original lines for round-trip inspection


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
