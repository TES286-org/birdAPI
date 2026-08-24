"""Parser for ``show interfaces`` and ``show interfaces summary``.

Wire example (``show interfaces``, BIRD 3.1.7)::

    1001-lo up (index=1)
    1004-\tMultiAccess AdminUp LinkUp Loopback Ignored MTU=65536
    1003-\t127.0.0.1/8 (Preferred, scope host)
     \t::1/128 (Preferred, scope host)
    1001-dummy0 down (index=2)
    1004-\tMultiAccess Broadcast Multicast AdminDown LinkDown MTU=1500
    1001-eth0 up (index=3)
    1004-\tMultiAccess Broadcast Multicast AdminUp LinkUp MTU=1450
    1003-\t21.0.15.216/32 (Preferred, scope univ)
     \tfe80::3859:dbff:fed6:ea22/64 (Preferred, scope link)
    0000

Section 1001 lines: ``<name> <up|down> (index=<n>[ master=<master>])``.
Section 1004 lines (optional, one per interface):
    ``\\t<Type> [Broadcast] [Multicast] Admin<Up|Down> Link<Up|Down> [Loopback] [Ignored] MTU=<mtu>``.
Section 1003 lines (zero or more per interface):
    ``\\t<ip>/<pxlen> (<flags>, scope <scope>)``.

Wire example (``show interfaces summary``, BIRD 3.1.7)::

    2005-Interface   State  IPv4 address       IPv6 address
    1005-lo         up     127.0.0.1/8        ::1/128
     dummy0     down
     eth0       up     21.0.15.216/32
    0000

The summary row layout uses space-padded columns; we parse by splitting
on 2+ space boundaries (the IPv4/IPv6 fields can be empty for some
rows).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import (
    InterfaceAddress,
    InterfaceOne,
    InterfacesResult,
    InterfacesSummaryResult,
    InterfaceSummaryRow,
)
from ..protocol import CommandResult

__all__ = ["parse_interfaces", "parse_interfaces_summary"]

# "lo up (index=1)" or "eth0 up (index=3 master=foo)"
_IFACE_LINE_RE = re.compile(
    r"^(?P<name>\S+)\s+(?P<state>up|down)\s+\(index=(?P<index>\d+)"
    r"(?:\s+master=(?P<master>\S+))?\)\s*$",
)
# "127.0.0.1/8 (Preferred, scope host)"
_ADDR_LINE_RE = re.compile(
    r"^\t?(?P<addr>\S+)\s+\((?P<flags>[^)]+)\)\s*$",
)

# Header for `show interfaces summary`:
#   "Interface   State  IPv4 address       IPv6 address"
_SUMMARY_HEADER_RE = re.compile(r"^Interface\s+State\s+IPv4 address\s+IPv6 address\s*$")

# A summary row: name (padded), state (up/dn), ipv4 or blank, ipv6 or blank.
# Use a regex with \S+ greedy columns separated by \s+ (matching the same
# robust pattern used for show protocols above).
_SUMMARY_ROW_RE = re.compile(
    r"^(?P<name>\S+)\s+(?P<state>up|dn)"
    r"(?:\s+(?P<ipv4>\S+))?"
    r"(?:\s+(?P<ipv6>\S+))?\s*$",
)


@dataclass
class _IfaceBuilder:
    """Mutable scratch for one interface. Frozen at the end of parsing."""

    name: str
    admin_state: str
    index: int | None
    master: str | None
    type_line: str = ""
    addresses: list[InterfaceAddress] = field(default_factory=list)

    def build(self) -> InterfaceOne:
        return InterfaceOne(
            name=self.name,
            admin_state=self.admin_state,
            index=self.index,
            master=self.master,
            type_line=self.type_line,
            addresses=list(self.addresses),
        )


def parse_interfaces(result: CommandResult) -> InterfacesResult:
    """Decode a ``show interfaces`` (non-summary) response."""
    interfaces: list[InterfaceOne] = []
    current: _IfaceBuilder | None = None

    for sec in result.sections:
        if sec.code == 1001:
            # Flush previous interface if any.
            if current is not None:
                interfaces.append(current.build())
                current = None
            # First line of the 1001 section is the name line.
            first_text = sec.lines[0].text if sec.lines else ""
            m = _IFACE_LINE_RE.match(first_text)
            if not m:
                # Unknown format — skip but keep parsing.
                continue
            current = _IfaceBuilder(
                name=m.group("name"),
                admin_state=m.group("state"),
                index=int(m.group("index")),
                master=m.group("master"),
            )
            continue

        if sec.code == 1004:
            if current is not None:
                # Single line of type info.
                line = sec.lines[0].text if sec.lines else ""
                current.type_line = line.lstrip("\t ")
            continue

        if sec.code == 1003:
            if current is None:
                continue
            for ln in sec.lines:
                text = ln.text
                m = _ADDR_LINE_RE.match(text)
                if not m:
                    continue
                addr = m.group("addr")
                flags = m.group("flags")
                scope: str | None = None
                if "scope" in flags:
                    # "..., scope host" -> "host"
                    sm = re.search(r"scope\s+(\S+)", flags)
                    if sm:
                        scope = sm.group(1)
                current.addresses.append(InterfaceAddress(address=addr, flags=flags, scope=scope))
            continue

    if current is not None:
        interfaces.append(current.build())

    return InterfacesResult(
        raw_text=result.raw_text,
        command=result.command,
        interfaces=interfaces,
    )


def parse_interfaces_summary(result: CommandResult) -> InterfacesSummaryResult:
    """Decode a ``show interfaces summary`` response."""
    rows: list[InterfaceSummaryRow] = []
    section_1005 = result.section(1005)
    if section_1005 is None:
        return InterfacesSummaryResult(
            raw_text=result.raw_text,
            command=result.command,
            rows=[],
        )
    for ln in section_1005.lines:
        text = ln.text
        if not text.strip():
            continue
        if _SUMMARY_HEADER_RE.match(text):
            continue
        m = _SUMMARY_ROW_RE.match(text)
        if not m:
            # Row may have empty ipv4 and ipv6 — try a more permissive parse.
            # The pattern allows them to be missing, so if we got here, the
            # row really is unparseable; skip it.
            continue
        rows.append(
            InterfaceSummaryRow(
                name=m.group("name"),
                state=m.group("state"),
                ipv4=m.group("ipv4") or "",
                ipv6=m.group("ipv6") or "",
            )
        )
    return InterfacesSummaryResult(
        raw_text=result.raw_text,
        command=result.command,
        rows=rows,
    )
