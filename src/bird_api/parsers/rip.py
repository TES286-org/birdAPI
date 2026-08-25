"""Parsers for the RIP ``show`` commands.

``show rip interfaces`` (code 1021, proto/rip/rip.c ``rip_show_interfaces``)::

    1021-rip_test:
     Interface  State  Metric   Nbrs   Timer
     eth0       Up          1      0  26.170
    0000

``show rip neighbors`` (code 1022, ``rip_show_neighbors``)::

    1022-rip_test:
     IP address                Interface  Metric Routes    Seen
     192.0.2.1                 eth0            1      3     12
    0000

Both commands answer a single ``<name>: is not up`` line when the protocol
is not running, which is reflected by ``up=False``.
"""

from __future__ import annotations

import re

from ..models import (
    RipInterfaceRow,
    RipInterfacesResult,
    RipNeighborRow,
    RipNeighborsResult,
)
from ..protocol import CommandResult

__all__ = ["parse_rip_interfaces", "parse_rip_neighbors"]

_NOT_UP_RE = re.compile(r"^(?P<name>\S+):\s+is not up\s*$")

# "%-10s %-6s %6u %6u %7t" — Interface, State, Metric, Nbrs, Timer
_IFACE_ROW_RE = re.compile(
    r"^(?P<iface>\S+)\s+(?P<state>\S+)\s+(?P<metric>\d+)\s+(?P<nbrs>\d+)\s+(?P<timer>\S+)\s*$",
)

# "%-25I %-10s %6u %6u %7t" — IP address, Interface, Metric, Routes, Seen
_NEIGH_ROW_RE = re.compile(
    r"^(?P<addr>\S+)\s+(?P<iface>\S+)\s+(?P<metric>\d+)\s+(?P<routes>\d+)\s+(?P<seen>\S+)\s*$",
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


def parse_rip_interfaces(result: CommandResult) -> RipInterfacesResult:
    """Decode a ``show rip interfaces`` response."""
    lines = _section_lines(result, 1021)
    protocol, up = _header(lines)
    rows: list[RipInterfaceRow] = []
    for text in lines[1:]:
        m = _IFACE_ROW_RE.match(text.strip())
        if m and m.group("state") in ("Up", "Down"):
            rows.append(
                RipInterfaceRow(
                    interface=m.group("iface"),
                    state=m.group("state"),
                    metric=_to_int(m.group("metric")),
                    neighbors=_to_int(m.group("nbrs")),
                    timer=m.group("timer"),
                ),
            )
    return RipInterfacesResult(
        raw_text=result.raw_text,
        command=result.command,
        protocol=protocol,
        up=up,
        rows=rows,
    )


def parse_rip_neighbors(result: CommandResult) -> RipNeighborsResult:
    """Decode a ``show rip neighbors`` response."""
    lines = _section_lines(result, 1022)
    protocol, up = _header(lines)
    rows: list[RipNeighborRow] = []
    for text in lines[1:]:
        m = _NEIGH_ROW_RE.match(text.strip())
        if m:
            rows.append(
                RipNeighborRow(
                    address=m.group("addr"),
                    interface=m.group("iface"),
                    metric=_to_int(m.group("metric")),
                    routes=_to_int(m.group("routes")),
                    seen=m.group("seen"),
                ),
            )
    return RipNeighborsResult(
        raw_text=result.raw_text,
        command=result.command,
        protocol=protocol,
        up=up,
        rows=rows,
    )
