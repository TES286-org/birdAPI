"""Parser for ``show static`` (code 1009).

Wire format (proto/static/static.c ``static_show_rt``)::

    1009-192.0.2.0/24\tblackhole
    1009-10.0.0.0/8
     \tvia 192.0.2.1 on eth0
     \tdev eth0 (dormant)
    1009-172.16.0.0/12\trecursive 192.0.2.1
    0000

A unicast route's prefix line carries no type; its nexthops follow on
``\tvia …`` / ``\tdev …`` continuation lines (one per ECMP path, possibly
marked ``onlink``, ``(bfd)`` or ``(dormant)``). Non-unicast routes carry
the type on the prefix line itself (``blackhole``, ``unreachable``,
``prohibit``, ``recursive <ip>``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import StaticResult, StaticRoute
from ..protocol import CommandResult

__all__ = ["parse_static"]

_PREFIX_LINE_RE = re.compile(r"^(?P<prefix>\S+)(?:\t(?P<dest>.*))?$")


@dataclass
class _RouteBuilder:
    """Mutable scratch for one static route."""

    prefix: str
    dest_type: str = "unicast"
    nexthops: list[str] = field(default_factory=list)

    def build(self) -> StaticRoute:
        return StaticRoute(
            prefix=self.prefix,
            dest_type=self.dest_type,
            nexthops=list(self.nexthops),
            dormant=any("(dormant)" in nh for nh in self.nexthops),
        )


def parse_static(result: CommandResult) -> StaticResult:
    """Decode a ``show static`` response."""
    builders: list[_RouteBuilder] = []
    current: _RouteBuilder | None = None

    for section in result.sections:
        if section.code != 1009:
            continue
        for ln in section.lines:
            text = ln.text  # keeps the leading tab on continuation lines
            if text.startswith("\t"):
                # Continuation: nexthop of the current unicast route.
                if current is not None:
                    current.nexthops.append(text.lstrip("\t"))
                continue
            m = _PREFIX_LINE_RE.match(text)
            if not m:
                continue
            if current is not None:
                builders.append(current)
            dest = (m.group("dest") or "").strip()
            if dest.startswith("recursive"):
                current = _RouteBuilder(
                    prefix=m.group("prefix"),
                    dest_type="recursive",
                    nexthops=[dest[len("recursive") :].strip()] if len(dest) > len("recursive") else [],
                )
            else:
                current = _RouteBuilder(prefix=m.group("prefix"), dest_type=dest or "unicast")
    if current is not None:
        builders.append(current)

    return StaticResult(
        raw_text=result.raw_text,
        command=result.command,
        routes=[b.build() for b in builders],
    )
