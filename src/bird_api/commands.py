"""Pure wire-command builders for every :class:`BirdClient` method.

Every function here takes exactly the parameters of the corresponding
client method, **validates** them via :mod:`bird_api.validation` and
returns the finished command string. Both the synchronous
(:class:`~bird_api.client.BirdClient`) and asynchronous
(:class:`~bird_api.async_client.AsyncBirdClient`) clients call these
builders, which guarantees that both stacks emit byte-identical wire
commands and share the same injection-prevention semantics — the parity
is additionally enforced by tests.

The functions are pure: no I/O, no globals, deterministic output.
"""

from __future__ import annotations

from .queries import RouteQuery
from .validation import (
    format_pattern,
    validate_address,
    validate_choice,
    validate_filter_expression,
    validate_identifier,
    validate_net_or_ip,
    validate_path,
    validate_pattern,
    validate_positive_int,
    validate_prefix,
)

__all__ = [
    "build_show_status",
    "build_show_memory",
    "build_show_protocols",
    "build_show_protocols_all",
    "build_get_protocol",
    "build_show_route",
    "build_show_route_query",
    "build_show_route_prefix",
    "build_show_route_for",
    "build_show_route_in",
    "build_show_route_table",
    "build_show_route_protocol",
    "build_show_route_stats",
    "build_show_symbols",
    "build_eval",
    "build_route_lookup",
    "build_route_exists",
    "build_count_routes",
    "build_bgp_sessions",
    "build_find_protocols",
    "build_configure",
    "build_enable",
    "build_disable",
    "build_restart",
    "build_reload",
    "build_dump",
    "build_show_static",
    "build_show_ospf",
    "build_show_ospf_neighbors",
    "build_show_ospf_interface",
    "build_show_ospf_state",
    "build_show_ospf_topology",
    "build_show_ospf_lsadb",
    "build_show_bfd_sessions",
    "build_show_rip_interfaces",
    "build_show_rip_neighbors",
    "build_show_babel_interfaces",
    "build_show_babel_neighbors",
    "build_show_mpls_ranges",
]

# ``show route``-family commands accept an optional protocol *pattern*
# (proto_patt2: a known symbol, the keyword ``all``, or a quoted TEXT).


def build_show_status() -> str:
    return "show status"


def build_show_memory() -> str:
    return "show memory"


def build_show_protocols(pattern: str | None = None) -> str:
    cmd = "show protocols"
    if pattern is not None:
        cmd += " " + format_pattern(pattern, field="pattern")
    return cmd


def build_show_protocols_all(pattern: str | None = None) -> str:
    cmd = "show protocols all"
    if pattern is not None:
        cmd += " " + format_pattern(pattern, field="pattern")
    return cmd


def build_get_protocol(name: str) -> str:
    return f"show protocols all {format_pattern(name, field='protocol name')}"


def build_show_route(*, all: bool = False) -> str:
    return "show route" + (" all" if all else "")


def build_show_route_query(query: RouteQuery) -> str:
    return query.build()


def build_show_route_prefix(prefix: str, *, all: bool = False) -> str:
    return f"show route {validate_prefix(prefix, field='prefix')}" + (" all" if all else "")


def build_show_route_for(address: str, *, all: bool = False) -> str:
    return f"show route for {validate_address(address, field='address')}" + (" all" if all else "")


def build_show_route_in(prefix: str, *, all: bool = False) -> str:
    return f"show route in {validate_prefix(prefix, field='prefix')}" + (" all" if all else "")


def build_show_route_table(table: str, *, all: bool = False) -> str:
    table = validate_identifier(table, field="table name")
    return f"show route table {table}" + (" all" if all else "")


def build_show_route_protocol(protocol: str, *, all: bool = False) -> str:
    protocol = validate_identifier(protocol, field="protocol name")
    return f"show route protocol {protocol}" + (" all" if all else "")


def build_show_route_stats() -> str:
    return "show route stats"


def build_show_symbols(*, table: bool = False) -> str:
    return "show symbols" + (" table" if table else "")


def build_eval(expr: str) -> str:
    return f"eval {validate_filter_expression(expr, field='expression')}"


def build_route_lookup(address: str, *, table: str | None = None, all: bool = False) -> str:
    query = RouteQuery().for_address(address)
    if table is not None:
        query = query.table(table)
    if all:
        query = query.all_details()
    return query.build()


def build_route_exists(prefix: str, *, table: str | None = None) -> str:
    query = RouteQuery().prefix(prefix)
    if table is not None:
        query = query.table(table)
    return query.build()


def build_count_routes(*, table: str | None = None) -> str:
    if table is not None:
        return RouteQuery().count().table(table).build()
    return RouteQuery().count().table_all().build()


def build_bgp_sessions(pattern: str | None = None) -> str:
    cmd = "show protocols all"
    if pattern is not None:
        cmd += " " + format_pattern(pattern, field="pattern")
    return cmd


def build_find_protocols(pattern: str) -> str:
    pattern = validate_pattern(pattern, field="pattern")
    return f"show protocols {format_pattern(pattern, field='pattern')}"


def build_configure(
    file: str,
    *,
    timeout: int | None = None,
    soft: bool = False,
    check: bool = False,
) -> str:
    # Per BIRD's lexer, the file path MUST be a double-quoted string;
    # internal double quotes are not supported by the lexer, so
    # validate_path rejects them.
    quoted = f'"{validate_path(file, field="config file path")}"'
    if check:
        return f"configure check {quoted}"
    soft_kw = "soft " if soft else ""
    if timeout is not None:
        seconds = validate_positive_int(timeout, field="timeout")
        return f"configure {soft_kw}{quoted} timeout {seconds}"
    return f"configure {soft_kw}{quoted}"


def build_enable(pattern: str = "all") -> str:
    return f"enable {format_pattern(pattern, field='pattern')}"


def build_disable(pattern: str = "all") -> str:
    return f"disable {format_pattern(pattern, field='pattern')}"


def build_restart(pattern: str = "all") -> str:
    return f"restart {format_pattern(pattern, field='pattern')}"


def build_reload(pattern: str = "all", *, direction: str = "both") -> str:
    direction = validate_choice(direction, ("both", "in", "out"), field="direction")
    wire = format_pattern(pattern, field="pattern")
    if direction == "both":
        return f"reload {wire}"
    return f"reload {direction} {wire}"


_DUMP_KINDS: frozenset[str] = frozenset({
    "resources", "sockets", "ao keys", "events", "interfaces",
    "neighbors", "attributes", "routes", "protocols", "filter all",
})


def build_dump(kind: str, file: str) -> str:
    kind = validate_choice(kind, _DUMP_KINDS, field="dump kind")
    path = validate_path(file, field="dump file path")
    return f'dump {kind} "{path}"'


# ---------------------------------------------------------------------------
# Protocol-specific show commands
# ---------------------------------------------------------------------------


def _opt_name(name: str | None, *, field: str = "protocol name") -> str:
    """Format an optional bare protocol symbol (``optproto`` grammar rule).

    ``optproto`` takes a *known symbol* — a bare identifier, not a pattern.
    """
    if name is None:
        return ""
    return " " + validate_identifier(name, field=field)


def _opt_iface(interface: str | None, *, field: str = "interface") -> str:
    """Format an optional double-quoted interface pattern (``opttext`` rule).

    ``opttext`` is a TEXT token, which BIRD's grammar only accepts in
    double quotes (verified live: ``show ospf interface eth0`` is a syntax
    error while ``show ospf interface "eth0"`` parses). The quoted value is
    matched with ``patmatch()`` — wildcards ``*`` / ``?`` are honoured.
    """
    if interface is None:
        return ""
    quoted = '"' + validate_pattern(interface, field=field) + '"'
    return " " + quoted


def build_show_static(pattern: str | None = None) -> str:
    return "show static" + _opt_name(pattern, field="protocol name")


def build_show_ospf(pattern: str | None = None) -> str:
    return "show ospf" + _opt_name(pattern, field="protocol name")


def build_show_ospf_neighbors(pattern: str | None = None, interface: str | None = None) -> str:
    return (
        "show ospf neighbors"
        + _opt_name(pattern, field="protocol name")
        + _opt_iface(interface)
    )


def build_show_ospf_interface(pattern: str | None = None, interface: str | None = None) -> str:
    return (
        "show ospf interface"
        + _opt_name(pattern, field="protocol name")
        + _opt_iface(interface)
    )


def build_show_ospf_state(pattern: str | None = None, *, all: bool = False) -> str:
    return "show ospf state" + (" all" if all else "") + _opt_name(pattern, field="protocol name")


def build_show_ospf_topology(pattern: str | None = None, *, all: bool = False) -> str:
    return "show ospf topology" + (" all" if all else "") + _opt_name(pattern, field="protocol name")


def build_show_ospf_lsadb(
    pattern: str | None = None,
    *,
    scope: str = "area",
    area: str | None = None,
    lsa_type: int | None = None,
    lsid: str | None = None,
    router: str | None = None,
    self_originated: bool = False,
) -> str:
    """Build ``show ospf lsadb`` with its filter options.

    Parameters mirror the ``lsadb_args`` grammar rule:
    ``[global | area <id> | link] [type <num>] [lsid <id>] [self | router <id>] [<proto>]``.
    """
    scope = validate_choice(scope, ("global", "area", "link"), field="scope")
    parts: list[str] = ["show ospf lsadb"]
    if scope == "global":
        parts.append("global")
    elif scope == "link":
        parts.append("link")
    elif area is not None:
        parts.append(f"area {validate_address(area, field='area ID')}")
    if lsa_type is not None:
        parts.append(f"type {validate_positive_int(lsa_type, field='LSA type', maximum=0xFFFF)}")
    if lsid is not None:
        parts.append(f"lsid {validate_address(lsid, field='LSA ID')}")
    if self_originated:
        parts.append("self")
    elif router is not None:
        parts.append(f"router {validate_address(router, field='router ID')}")
    parts.append(_opt_name(pattern, field="protocol name").lstrip())
    return " ".join(p for p in parts if p)


def build_show_bfd_sessions(
    pattern: str | None = None,
    *,
    address: str | None = None,
    interface: str | None = None,
    family: str | None = None,
    mode: str | None = None,
    all: bool = False,
) -> str:
    """Build ``show bfd sessions`` with its filters.

    Parameters mirror ``bfd_show_sessions_args``:
    ``[<name>] [address <ip|prefix>] [(interface|dev) "<name>"] [ipv4|ipv6]
    [direct|multihop] [all]``.
    """
    if family is not None:
        family = validate_choice(family, ("ipv4", "ipv6"), field="family")
    if mode is not None:
        mode = validate_choice(mode, ("direct", "multihop"), field="mode")
    parts: list[str] = ["show bfd sessions"]
    if pattern is not None:
        parts.append(validate_identifier(pattern, field="protocol name"))
    if address is not None:
        parts.append(f"address {validate_net_or_ip(address)}")
    if interface is not None:
        # INTERFACE/DEV take a TEXT token — always double-quoted on the wire.
        quoted = '"' + validate_pattern(interface, field="interface") + '"'
        parts.append(f"interface {quoted}")
    if family is not None:
        parts.append(family)
    if mode is not None:
        parts.append(mode)
    if all:
        parts.append("all")
    return " ".join(parts)


def build_show_rip_interfaces(pattern: str | None = None, interface: str | None = None) -> str:
    return (
        "show rip interfaces"
        + _opt_name(pattern, field="protocol name")
        + _opt_iface(interface)
    )


def build_show_rip_neighbors(pattern: str | None = None, interface: str | None = None) -> str:
    return (
        "show rip neighbors"
        + _opt_name(pattern, field="protocol name")
        + _opt_iface(interface)
    )


def build_show_babel_interfaces(pattern: str | None = None, interface: str | None = None) -> str:
    return (
        "show babel interfaces"
        + _opt_name(pattern, field="protocol name")
        + _opt_iface(interface)
    )


def build_show_babel_neighbors(pattern: str | None = None, interface: str | None = None) -> str:
    return (
        "show babel neighbors"
        + _opt_name(pattern, field="protocol name")
        + _opt_iface(interface)
    )


def build_show_mpls_ranges() -> str:
    return "show mpls ranges"
