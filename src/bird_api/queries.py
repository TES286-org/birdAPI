"""Typed, validated builders for BIRD's more complex queries.

Two public classes live here:

* :class:`RouteQuery` — a fluent builder covering the full ``show route``
  grammar (``nest/config.Y``, rule ``r_args``):

  ::

     show route [[<prefix> | for <prefix> | for <ip> | in <prefix>]
                [table <t> … | table all | (import|export) table <p>[.<c>]]
                [filter <f> | where <cond>]
                [all] [primary] [filtered]
                [(export|preexport|noexport) <p>[.<c>]]
                [protocol <p>]
                [stats | count]]

* :class:`RouteFilter` — a small algebra of **validated** filter expressions
  for ``where`` clauses, with ``&`` / ``|`` / ``~`` (not) composition and
  leaf constructors for the common cases (protocol match, prefix-set
  membership, prefix length, BGP communities).

Both classes are immutable: every builder method returns a new instance,
so queries can be shared and refined safely across threads. Every user
input is validated at build time via :mod:`bird_api.validation` — a query
that builds successfully cannot inject commands into the control socket.

Example
-------
    >>> from bird_api import BirdClient, RouteQuery, RouteFilter
    >>> with BirdClient(socket_path="/tmp/bird.ctl") as client:
    ...     q = (
    ...         RouteQuery()
    ...         .table("master4")
    ...         .where(RouteFilter.protocol("bgp1") & RouteFilter.prefix_len(min=16))
    ...         .all_details()
    ...     )
    ...     result = client.show_route_query(q)
    ...     for entry in result.entries:
    ...         print(entry.prefix, entry.attributes.get("bgp_next_hop"))
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, replace
from typing import Literal

from .validation import (
    check_command_length,
    validate_address,
    validate_channel_arg,
    validate_filter_expression,
    validate_identifier,
    validate_positive_int,
    validate_prefix,
)

__all__ = ["RouteQuery", "RouteFilter"]


# ---------------------------------------------------------------------------
# RouteFilter — validated filter-expression algebra
# ---------------------------------------------------------------------------


class RouteFilter:
    """A validated BIRD filter expression for ``where`` clauses.

    Instances are built either from typed leaf constructors (which validate
    their inputs and therefore cannot inject) or via
    :meth:`RouteFilter.raw` for arbitrary expressions (single line of
    printable ASCII — see
    :func:`~bird_api.validation.validate_filter_expression`).

    Filters compose with ``&`` (logical AND), ``|`` (logical OR) and ``~``
    (logical NOT; unary) following BIRD's C-style operators (``&&``, ``||``,
    ``!`` — verified against BIRD 3.1.7; the word forms ``and``/``or`` are
    NOT accepted by the daemon).
    """

    __slots__ = ("_expr",)

    def __init__(self, expression: str) -> None:
        self._expr = validate_filter_expression(expression, field="filter expression")

    # -- leaf constructors ---------------------------------------------------

    @classmethod
    def raw(cls, expression: str) -> RouteFilter:
        """Wrap an arbitrary (but single-line, printable-ASCII) expression."""
        return cls(expression)

    @classmethod
    def protocol(cls, name: str) -> RouteFilter:
        """``proto = "<name>"`` — route was learned from protocol ``name``."""
        name = validate_identifier(name, field="protocol name")
        return cls(f'proto = "{name}"')

    @classmethod
    def prefix(cls, net: str | ipaddress.IPv4Network | ipaddress.IPv6Network) -> RouteFilter:
        """``net = <prefix>`` — exact prefix match."""
        net = validate_prefix(net, field="prefix")
        return cls(f"net = {net}")

    @classmethod
    def prefix_in(
        cls,
        nets: str | ipaddress.IPv4Network | ipaddress.IPv6Network
        | list[str | ipaddress.IPv4Network | ipaddress.IPv6Network],
        *,
        or_longer: bool = False,
    ) -> RouteFilter:
        """``net ~ [p1, p2, …]`` — route's prefix is a member of the set.

        With ``or_longer=True`` every set member gets BIRD's ``+`` suffix,
        matching the prefix *and all its subnets* (``10.0.0.0/8+``).
        """
        if isinstance(nets, str | ipaddress.IPv4Network | ipaddress.IPv6Network):
            nets = [nets]
        if not nets:
            raise ValueError("prefix_in() requires at least one prefix")
        canonical = [
            validate_prefix(n, field="prefix") + ("+" if or_longer else "") for n in nets
        ]
        return cls(f"net ~ [{', '.join(canonical)}]")

    @classmethod
    def prefix_len(cls, *, min: int | None = None, max: int | None = None) -> RouteFilter:
        """``net.len >= min && net.len <= max`` — prefix-length range filter."""
        if min is None and max is None:
            raise ValueError("prefix_len() requires min and/or max")
        parts: list[str] = []
        if min is not None:
            parts.append(f"net.len >= {validate_positive_int(min, field='min prefix length', maximum=128)}")
        if max is not None:
            parts.append(f"net.len <= {validate_positive_int(max, field='max prefix length', maximum=128)}")
        if len(parts) == 1:
            return cls(parts[0])
        return cls(f"({parts[0]} && {parts[1]})")

    @classmethod
    def community(cls, asn: int, value: int) -> RouteFilter:
        """``(asn, value) ~ bgp_community`` — standard BGP community present."""
        asn = validate_positive_int(asn, field="community ASN", maximum=0xFFFFFFFF)
        value = validate_positive_int(value, field="community value", maximum=0xFFFF)
        return cls(f"({asn}, {value}) ~ bgp_community")

    @classmethod
    def large_community(cls, asn: int, part1: int, part2: int) -> RouteFilter:
        """``(asn, p1, p2) ~ bgp_large_community`` — large community present."""
        asn = validate_positive_int(asn, field="large-community ASN", maximum=0xFFFFFFFF)
        part1 = validate_positive_int(part1, field="large-community part 1", maximum=0xFFFFFFFF)
        part2 = validate_positive_int(part2, field="large-community part 2", maximum=0xFFFFFFFF)
        return cls(f"({asn}, {part1}, {part2}) ~ bgp_large_community")

    # -- composition ----------------------------------------------------------

    def __and__(self, other: object) -> RouteFilter:
        if not isinstance(other, RouteFilter):
            return NotImplemented
        return RouteFilter(f"({self._expr} && {other._expr})")

    def __or__(self, other: object) -> RouteFilter:
        if not isinstance(other, RouteFilter):
            return NotImplemented
        return RouteFilter(f"({self._expr} || {other._expr})")

    def __invert__(self) -> RouteFilter:
        return RouteFilter(f"!({self._expr})")

    # -- inspection ------------------------------------------------------------

    @property
    def expression(self) -> str:
        """The validated wire form of this filter."""
        return self._expr

    def __str__(self) -> str:
        return self._expr

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"RouteFilter({self._expr!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RouteFilter):
            return NotImplemented
        return self._expr == other._expr

    def __hash__(self) -> int:
        return hash(self._expr)


# ---------------------------------------------------------------------------
# RouteQuery — validated `show route` builder
# ---------------------------------------------------------------------------

AddrMode = Literal["exact", "for", "in"]
ExportMode = Literal["export", "preexport", "noexport"]
StatsMode = Literal["stats", "count"]


@dataclass(frozen=True, slots=True)
class RouteQuery:
    """Immutable builder for the ``show route`` command family.

    Each method validates its input *immediately* and returns a **new**
    :class:`RouteQuery`; the receiver is never modified. Mutual-exclusion
    rules implied by BIRD's grammar (e.g. only one address selector, filter
    XOR where, stats XOR count) are enforced when :meth:`build` assembles
    the final command, with clear client-side error messages instead of a
    remote ``9001 syntax error``.
    """

    addr_mode: AddrMode | None = None
    addr: str | None = None
    tables: tuple[str, ...] = ()  # validated identifiers
    all_tables: bool = False
    import_channel: str | None = None  # (import|export) table <p>[.<c>]
    export_channel_table: str | None = None
    filter_symbol: str | None = None  # named filter (field renamed: method owns the public name)
    where_expr: RouteFilter | None = None
    verbose: bool = False  # 'all'
    primary_only: bool = False  # 'primary'
    filtered: bool = False  # 'filtered'
    export_mode: ExportMode | None = None
    export_target: str | None = None
    protocol_name: str | None = None
    stats_mode: StatsMode | None = None

    # -- address selectors (mutually exclusive) --------------------------------

    def _set_addr(self, mode: AddrMode, addr: str) -> RouteQuery:
        """Set the address selector, rejecting a second selector (BIRD:
        "Only one prefix expected")."""
        if self.addr is not None:
            raise ValueError(
                "only one address selector allowed (BIRD: 'Only one prefix expected'); "
                f"already selected {self.addr_mode} {self.addr!r}"
            )
        return replace(self, addr_mode=mode, addr=addr)

    def prefix(self, net: str | ipaddress.IPv4Network | ipaddress.IPv6Network) -> RouteQuery:
        """``show route <prefix>`` — exact network match."""
        return self._set_addr("exact", validate_prefix(net, field="prefix"))

    def for_prefix(self, net: str | ipaddress.IPv4Network | ipaddress.IPv6Network) -> RouteQuery:
        """``show route for <prefix>`` — best routes covering the prefix."""
        return self._set_addr("for", validate_prefix(net, field="prefix"))

    def for_address(self, ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> RouteQuery:
        """``show route for <ip>`` — routing-table lookup for one address."""
        return self._set_addr("for", validate_address(ip, field="address"))

    def within(self, net: str | ipaddress.IPv4Network | ipaddress.IPv6Network) -> RouteQuery:
        """``show route in <prefix>`` — all routes contained within the prefix."""
        return self._set_addr("in", validate_prefix(net, field="prefix"))

    # -- table selectors ---------------------------------------------------------

    def table(self, name: str) -> RouteQuery:
        """Add ``table <name>``. May be called repeatedly for multiple tables."""
        name = validate_identifier(name, field="table name")
        return replace(self, tables=(*self.tables, name))

    def table_all(self) -> RouteQuery:
        """``table all`` — walk every routing table."""
        return replace(self, all_tables=True)

    def import_table(self, channel: str) -> RouteQuery:
        """``import table <p>[.<c>]`` — a protocol channel's import table."""
        return replace(self, import_channel=validate_channel_arg(channel, field="channel"))

    def export_table(self, channel: str) -> RouteQuery:
        """``export table <p>[.<c>]`` — a protocol channel's export table."""
        return replace(self, export_channel_table=validate_channel_arg(channel, field="channel"))

    # -- filters ------------------------------------------------------------------

    def filter_name(self, name: str) -> RouteQuery:
        """``filter <name>`` — use a named filter defined in the config."""
        return replace(self, filter_symbol=validate_identifier(name, field="filter name"))

    def where(self, cond: RouteFilter | str) -> RouteQuery:
        """``where <cond>`` — inline filter expression.

        Accepts a :class:`RouteFilter` (preferred) or a raw string (validated
        to a single printable-ASCII line).
        """
        if isinstance(cond, RouteFilter):
            return replace(self, where_expr=cond)
        return replace(self, where_expr=RouteFilter(cond))

    # -- output shaping ------------------------------------------------------------

    def all_details(self) -> RouteQuery:
        """``all`` — dump every route's attribute block."""
        return replace(self, verbose=True)

    def primary(self) -> RouteQuery:
        """``primary`` — only best (primary) routes."""
        return replace(self, primary_only=True)

    def include_filtered(self) -> RouteQuery:
        """``filtered`` — include routes rejected by import filters."""
        return replace(self, filtered=True)

    def protocol(self, name: str) -> RouteQuery:
        """``protocol <name>`` — only routes from this protocol."""
        return replace(self, protocol_name=validate_identifier(name, field="protocol name"))

    # -- export simulation ------------------------------------------------------------

    def export(self, target: str) -> RouteQuery:
        """``export <p>[.<c>]`` — routes this protocol/channel would export."""
        return replace(self, export_mode="export", export_target=validate_channel_arg(target))

    def preexport(self, target: str) -> RouteQuery:
        """``preexport <p>[.<c>]`` — routes before the export filter runs."""
        return replace(self, export_mode="preexport", export_target=validate_channel_arg(target))

    def noexport(self, target: str) -> RouteQuery:
        """``noexport <p>[.<c>]`` — routes rejected by the export filter."""
        return replace(self, export_mode="noexport", export_target=validate_channel_arg(target))

    # -- statistics ------------------------------------------------------------------

    def _set_stats(self, mode: StatsMode) -> RouteQuery:
        if self.stats_mode is not None and self.stats_mode != mode:
            raise ValueError("cannot combine stats() with count() — pick one")
        return replace(self, stats_mode=mode)

    def stats(self) -> RouteQuery:
        """``stats`` — route statistics table."""
        return self._set_stats("stats")

    def count(self) -> RouteQuery:
        """``count`` — route counts only (no route lines)."""
        return self._set_stats("count")

    # -- assembly -----------------------------------------------------------------------

    def build(self) -> str:
        """Assemble the validated wire command. Raises on rule violations."""
        parts: list[str] = ["show route"]

        if self.addr is not None:
            mode = self.addr_mode
            if mode == "exact":
                parts.append(self.addr)
            elif mode == "for":
                parts.extend(("for", self.addr))
            elif mode == "in":
                parts.extend(("in", self.addr))
            else:  # pragma: no cover - defensive
                raise ValueError(f"unknown addr mode {mode!r}")

        if self.tables and self.all_tables:
            raise ValueError("cannot combine table(<name>) with table_all()")
        if self.tables and (self.import_channel or self.export_channel_table):
            raise ValueError("cannot combine table(<name>) with import/export table")
        if self.import_channel and self.export_channel_table:
            raise ValueError("cannot combine import_table() with export_table()")
        if self.tables:
            parts.extend(f"table {t}" for t in self.tables)
        if self.all_tables:
            parts.append("table all")
        if self.import_channel:
            parts.append(f"import table {self.import_channel}")
        if self.export_channel_table:
            parts.append(f"export table {self.export_channel_table}")

        if self.filter_symbol and self.where_expr is not None:
            raise ValueError("cannot combine filter_name() with where() (BIRD: 'Filter specified twice')")
        if self.filter_symbol:
            parts.append(f"filter {self.filter_symbol}")
        if self.where_expr is not None:
            parts.append(f"where {self.where_expr.expression}")

        if self.verbose:
            parts.append("all")
        if self.primary_only:
            parts.append("primary")
        if self.filtered:
            parts.append("filtered")

        if self.export_mode is not None:
            parts.append(f"{self.export_mode} {self.export_target}")

        if self.protocol_name:
            parts.append(f"protocol {self.protocol_name}")

        if self.stats_mode is not None:
            parts.append(self.stats_mode)

        return check_command_length(" ".join(parts))

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        try:
            return f"RouteQuery({self.build()!r})"
        except ValueError:
            return "RouteQuery(<invalid>)"
