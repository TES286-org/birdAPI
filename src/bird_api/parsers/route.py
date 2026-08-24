"""Parser for the ``show route`` family of commands.

Wire example (``show route``)::

    1007-Table master4:
     192.0.2.0/24         blackhole [static_test 13:22:41.388] ! (200)
     198.51.100.0/24      blackhole [static_test 13:22:41.388] ! (200)
     0.0.0.0/0            unicast [kernel1 13:22:42.389] * (10)
     \tvia 21.0.0.1 on eth0 onlink
    0000

The route line format is::

    <prefix> <dest-name> [<protocol> <timestamp>] <primary> (<preference>)

* ``prefix`` is left-padded with one space (continuation prefix stripped).
* ``dest-name`` is the route type, e.g. ``blackhole``, ``unicast``, ``unreachable``.
* ``[<protocol> <timestamp>]`` is the route's source.
* ``primary`` is ``*`` for the primary route, ``!`` for a sync error.
* ``(200)`` is the preference.

Nexthop continuation lines start with a tab: ``\\tvia <gw> on <iface> [onlink] [weight N]``.
``show route all`` adds a ``1012-`` section after each route carrying
``\\t<attribute>: <value>`` lines (e.g. ``preference``, ``source``, ``krt_metric``).
After the 1012 section may come a ``1008-`` section with one line of
internal route-handling values.

``show route stats`` ends with a ``0014 Total:`` line and per-table
summary lines (``<n> of <n> routes for <n> networks in table <name>``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import RouteEntry, RouteResult
from ..protocol import CommandResult

__all__ = ["parse_route"]

# One route entry main line:
#   "192.0.2.0/24         blackhole [static_test 13:22:41.388] ! (200)"
# The prefix is space-padded so it column-aligns with siblings; we split
# on the 2+ space boundary between prefix and dest-name.
_ROUTE_LINE_RE = re.compile(
    r"^(?P<prefix>\S+)\s{2,}(?P<dest>\S+)\s+"
    r"\[(?P<proto>\S+)\s+(?P<ts>[^\]]+)\]\s+"
    r"(?P<primary>[*!])\s+"
    r"\((?P<pref>\d+)\)\s*$",
)

# Continuation line: "\tvia <gw> on <iface> [onlink] [weight N] [mpls ...]"
_VIA_RE = re.compile(r"^via\s+(.+)$")
# Continuation line: "\tdev <iface> [mpls ...]"
_DEV_RE = re.compile(r"^dev\s+(.+)$")

# show route stats per-table summary line:
#   "6 of 6 routes for 6 networks in table master4"
_PER_TABLE_STATS_RE = re.compile(
    r"^(?P<routes>\d+)\s+of\s+(?P<routes_total>\d+)\s+routes\s+for\s+"
    r"(?P<networks>\d+)\s+networks\s+in\s+table\s+(?P<table>\S+)\s*$",
)

# show route stats total line:
#   "Total: 6 of 6 routes for 6 networks in 2 tables"
_TOTAL_STATS_RE = re.compile(
    r"^Total:\s+(?P<routes>\d+)\s+of\s+(?P<routes_total>\d+)\s+routes\s+for\s+"
    r"(?P<networks>\d+)\s+networks\s+in\s+(?P<tables>\d+)\s+tables\s*$",
)

# show route per-table header line: "Table master4:"
_TABLE_HEADER_RE = re.compile(r"^Table\s+(\S+?):\s*$")


@dataclass
class _EntryBuilder:
    """Mutable scratch for one route entry. Converted to a frozen RouteEntry at the end."""

    prefix: str
    destination: str = ""
    protocol: str = ""
    timestamp: str = ""
    primary: bool = False
    via: list[str] = field(default_factory=list)
    dev: list[str] = field(default_factory=list)
    attribute_lines: list[str] = field(default_factory=list)
    internal_handling: str | None = None
    raw_lines: list[str] = field(default_factory=list)

    def build(self) -> RouteEntry:
        return RouteEntry(
            prefix=self.prefix,
            destination=self.destination,
            protocol=self.protocol,
            timestamp=self.timestamp,
            primary=self.primary,
            via=list(self.via),
            dev=list(self.dev),
            attribute_lines=list(self.attribute_lines),
            raw_lines=list(self.raw_lines),
        )


def parse_route(result: CommandResult) -> RouteResult:
    """Decode a ``show route`` / ``show route all`` / ``show route stats`` response.

    The parser walks sections in arrival order and uses a small state
    machine to associate 1012 (attribute) and 1008 (internal-handling)
    sections with the most recent 1007 route entry. The first 1007 section
    carries the table name in its first line ("``Table master4:``"); subsequent
    1007 sections are pure route entries.
    """
    table_name = ""
    builders: list[_EntryBuilder] = []
    current: _EntryBuilder | None = None
    stats: dict[str, str] = {}

    def _flush_current() -> None:
        nonlocal current
        if current is not None:
            builders.append(current)
            current = None

    for sec in result.sections:
        if sec.code == 1007:
            _flush_current()
            # First line of the first 1007 section is "Table <name>:".
            # In `show route stats`, a per-table header may also appear
            # mid-response when multiple tables are walked.
            lines_iter = iter(sec.lines)
            for ln in lines_iter:
                text = ln.text.lstrip()
                # Table header (first 1007 line).
                m_table = _TABLE_HEADER_RE.match(text)
                if m_table:
                    if not table_name:
                        table_name = m_table.group(1)
                    # Skip the rest of this section's table-summary line for stats
                    # (handled below by _PER_TABLE_STATS_RE).
                    continue
                # Per-table summary line for `show route stats`.
                m_stats = _PER_TABLE_STATS_RE.match(text)
                if m_stats:
                    stats[f"{m_stats.group('table')}.routes"] = m_stats.group("routes")
                    stats[f"{m_stats.group('table')}.routes_total"] = m_stats.group("routes_total")
                    stats[f"{m_stats.group('table')}.networks"] = m_stats.group("networks")
                    continue
                # A new route entry.
                m_route = _ROUTE_LINE_RE.match(text)
                if m_route:
                    if current is not None:
                        builders.append(current)
                    current = _EntryBuilder(
                        prefix=m_route.group("prefix"),
                        destination=m_route.group("dest"),
                        protocol=m_route.group("proto"),
                        timestamp=m_route.group("ts"),
                        primary=(m_route.group("primary") == "*"),
                    )
                    current.raw_lines.append(text)
                    continue
                # Continuation of the current entry.
                if current is None:
                    # Skip orphan lines silently — they're usually blank
                    # separators that the section grouping already absorbed.
                    continue
                # via or dev continuation (tab-prefixed in raw, lstripped here).
                m_via = _VIA_RE.match(text)
                if m_via:
                    current.via.append(text)
                    current.raw_lines.append(text)
                    continue
                m_dev = _DEV_RE.match(text)
                if m_dev:
                    current.dev.append(text)
                    current.raw_lines.append(text)
                    continue
                # Unknown continuation: stash it on raw_lines for round-trip.
                current.raw_lines.append(text)
            # End of section. The current entry may receive 1012/1008 sections
            # before the next 1007 section starts; defer flushing until then.
            continue

        if sec.code == 1012:
            if current is None:
                continue
            for ln in sec.lines:
                text = ln.text
                # Strip the leading tab from "\tpreference: 200" -> "preference: 200"
                stripped = text.lstrip("\t ")
                current.attribute_lines.append(stripped)
                current.raw_lines.append(stripped)
            continue

        if sec.code == 1008:
            if current is None:
                continue
            line = sec.lines[0].text if sec.lines else ""
            current.internal_handling = line.lstrip("\t ")
            current.raw_lines.append(line.lstrip("\t "))
            continue

        if sec.code == 14:
            text = sec.lines[0].text if sec.lines else ""
            m_total = _TOTAL_STATS_RE.match(text)
            if m_total:
                stats["routes"] = m_total.group("routes")
                stats["routes_total"] = m_total.group("routes_total")
                stats["networks"] = m_total.group("networks")
                stats["tables"] = m_total.group("tables")
            else:
                stats["total_line"] = text.strip()
            continue

        # Other sections (0000 final, 8001 Network not found, etc.) are ignored
        # here — they have no entries to attach. The 8001 case will have been
        # raised as BirdCommandError by the connection layer before we get here.

    _flush_current()
    entries = [b.build() for b in builders]

    return RouteResult(
        raw_text=result.raw_text,
        command=result.command,
        table=table_name,
        entries=entries,
        stats=stats,
    )
