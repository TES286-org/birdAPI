"""Parsers for the Babel ``show`` commands.

``show babel interfaces`` (code 1023, proto/babel/babel.c ``babel_show_interfaces``)::

    1023-babel_test:
     Interface  State  Auth  RX cost   Nbrs   Timer Next hop (v4)   Next hop (v6)
     eth0       Up     No         96      0   1.535 21.0.22.68      fe80::3452:3bff:fe15:9910
    0000

``show babel neighbors`` (code 1024, ``babel_show_neighbors``)::

    1024-babel_test:
     IP address                Interface  Metric Routes Hellos Expires Auth  RTT (ms)
     fe80::1                   eth0          96      3      3     9 No            3.4
    0000

Both commands answer a single ``<name>: is not up`` line when the protocol
is not running, which is reflected by ``up=False``.
"""

from __future__ import annotations

import re

from ..models import (
    BabelInterfaceRow,
    BabelInterfacesResult,
    BabelNeighborRow,
    BabelNeighborsResult,
)
from ..protocol import CommandResult

__all__ = ["parse_babel_interfaces", "parse_babel_neighbors"]

_NOT_UP_RE = re.compile(r"^(?P<name>\S+):\s+is not up\s*$")

# "%-10s %-6s %-5s %7u %6u %7t %-15I %I"
# Interface, State, Auth, RX cost, Nbrs, Timer, Next hop (v4), Next hop (v6)
_IFACE_ROW_RE = re.compile(
    r"^(?P<iface>\S+)\s+(?P<state>Up|Down)\s+(?P<auth>\S+)\s+(?P<rxcost>\d+)\s+"
    r"(?P<nbrs>\d+)\s+(?P<timer>\S+)\s+(?P<nh4>\S+)\s+(?P<nh6>\S+)\s*$",
)

# "%-25I %-10s %6u %6u %6u %7t %-4s %9t"
# IP address, Interface, Metric, Routes, Hellos, Expires, Auth, RTT (ms)
_NEIGH_ROW_RE = re.compile(
    r"^(?P<addr>\S+)\s+(?P<iface>\S+)\s+(?P<metric>\d+)\s+(?P<routes>\d+)\s+"
    r"(?P<hellos>\d+)\s+(?P<expires>\S+)\s+(?P<auth>\S+)\s+(?P<rtt>\S+)\s*$",
)


def _header(lines: list[str]) -> tuple[str, bool]:
    """Extract (protocol name, up flag) from the first line."""
    if not lines:
        return "", True
    first = lines[0].strip()
    m = _NOT_UP_RE.match(first)
    if m:
        return m.group("name"), False
    if first.endswith(":"):
        return first[:-1], True
    return first, True


def _section_lines(result: CommandResult, code: int) -> list[str]:
    section = result.section(code)
    if section is None:
        return []
    return [ln.text for ln in section.lines]


def _to_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def parse_babel_interfaces(result: CommandResult) -> BabelInterfacesResult:
    """Decode a ``show babel interfaces`` response."""
    lines = _section_lines(result, 1023)
    protocol, up = _header(lines)
    rows: list[BabelInterfaceRow] = []
    for text in lines[1:]:
        m = _IFACE_ROW_RE.match(text.strip())
        if m:
            rows.append(
                BabelInterfaceRow(
                    interface=m.group("iface"),
                    state=m.group("state"),
                    auth=m.group("auth"),
                    rxcost=_to_int(m.group("rxcost")),
                    neighbors=_to_int(m.group("nbrs")),
                    timer=m.group("timer"),
                    next_hop_v4=m.group("nh4"),
                    next_hop_v6=m.group("nh6"),
                ),
            )
    return BabelInterfacesResult(
        raw_text=result.raw_text,
        command=result.command,
        protocol=protocol,
        up=up,
        rows=rows,
    )


def parse_babel_neighbors(result: CommandResult) -> BabelNeighborsResult:
    """Decode a ``show babel neighbors`` response."""
    lines = _section_lines(result, 1024)
    protocol, up = _header(lines)
    rows: list[BabelNeighborRow] = []
    for text in lines[1:]:
        m = _NEIGH_ROW_RE.match(text.strip())
        if m:
            rows.append(
                BabelNeighborRow(
                    address=m.group("addr"),
                    interface=m.group("iface"),
                    metric=_to_int(m.group("metric")),
                    routes=_to_int(m.group("routes")),
                    hellos=_to_int(m.group("hellos")),
                    expires=m.group("expires"),
                    auth=m.group("auth"),
                    rtt_ms=m.group("rtt"),
                ),
            )
    return BabelNeighborsResult(
        raw_text=result.raw_text,
        command=result.command,
        protocol=protocol,
        up=up,
        rows=rows,
    )
