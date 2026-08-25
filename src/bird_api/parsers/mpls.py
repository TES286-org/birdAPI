"""Parser for ``show mpls ranges`` (code 1026).

Wire format (nest/mpls.c ``mpls_show_ranges``)::

    1026-MPLS domain test_dom:
     Range         Start  Length     End  Labels    Last
     static           16     984    1000       0       0
     dynamic        1000    9000   10000       0       0
    1026-
    1026-MPLS domain other:
     ...
    0000

Multiple domains are separated by a blank 1026 line. ``static`` and
``dynamic`` are the implicit built-in ranges; user ranges use their
configured names.
"""

from __future__ import annotations

import re

from ..models import MplsDomainRanges, MplsRangeRow, MplsRangesResult
from ..protocol import CommandResult

__all__ = ["parse_mpls_ranges"]

_DOMAIN_HEADER_RE = re.compile(r"^MPLS domain\s+(.+?):\s*$")
_COLUMN_HEADER_RE = re.compile(r"^Range\s+Start\s+Length\s+End\s+Labels\s+Last\s*$")
_ROW_RE = re.compile(
    r"^(?P<name>\S+)\s+(?P<start>\d+)\s+(?P<length>\d+)\s+(?P<end>\d+)\s+"
    r"(?P<labels>\d+)\s+(?P<last>\d+)\s*$",
)


def _to_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def parse_mpls_ranges(result: CommandResult) -> MplsRangesResult:
    """Decode a ``show mpls ranges`` response."""
    domains: list[MplsDomainRanges] = []
    current: MplsDomainRanges | None = None

    for section in result.sections:
        if section.code != 1026:
            continue
        for ln in section.lines:
            text = ln.text.strip()
            if not text:
                continue
            m = _DOMAIN_HEADER_RE.match(text)
            if m:
                if current is not None:
                    domains.append(current)
                current = MplsDomainRanges(domain=m.group(1).strip(), rows=[])
                continue
            if _COLUMN_HEADER_RE.match(text) or current is None:
                continue
            m = _ROW_RE.match(text)
            if m:
                current.rows.append(
                    MplsRangeRow(
                        name=m.group("name"),
                        start=_to_int(m.group("start")),
                        length=_to_int(m.group("length")),
                        end=_to_int(m.group("end")),
                        labels=_to_int(m.group("labels")),
                        last=_to_int(m.group("last")),
                    ),
                )
    if current is not None:
        domains.append(current)

    return MplsRangesResult(raw_text=result.raw_text, command=result.command, domains=domains)
