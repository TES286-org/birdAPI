"""Parser for the ``show memory`` command.

Wire example (BIRD 3.1.7)::

    1018-BIRD memory usage
                       Effective    Overhead
     Routing tables:    147.1 kB     11.5 kB
     Route attributes:   72.5 kB     14.8 kB
     Protocols:           9.8 kB   4640.0  B
     Current config:   8351.0  B    504.0  B
     Standby memory:      0.0  B    240.0 kB
     Total:             376.2 kB    316.3 kB

     Active pages:      144.0 kB
     Kept free pages:   240.0 kB
     Cold free pages:     0.0  B
    1028-Hot page cache depleted while in RCU: 0
    0000

Field layout (``nest/cmds.c:108``): ``%-17s " SIZE_FORMAT "   " SIZE_FORMAT``
where ``SIZE_FORMAT = "% 4u.%1u % 1cB"`` (4-digit integer, 1 decimal, 1 unit
suffix from ``" kMGT"``). The first column is the label + ``:``. The first
row of the table is the column header (``Effective`` / ``Overhead``) and
carries no label. ``Active pages`` / ``Kept free pages`` / ``Cold free
pages`` carry only an effective value (no overhead column).
"""

from __future__ import annotations

import re

from ..models import MemoryInfo, MemoryRow
from ..protocol import CommandResult

__all__ = ["parse_memory"]

# Match a single BIRD size value, e.g. "147.1 kB", "8351.0  B", "0.0  B".
# The format string from the source is "% 4u.%1u % 1cB" — the numeric
# part can be space-padded and the unit suffix can be "B" (single char)
# or " kB" / "MB" / "GB" / "TB" (one of " kMGT" followed by B). The
# number and unit can be separated by 1 or 2 spaces.
_SIZE_RE = re.compile(r"\d+\.\d+\s+[ kMGT]?B")

# Match "Routing tables:" etc.
_LABELED_ROW_RE = re.compile(r"^(?P<label>.+?):\s+(?P<rest>.+)$")


def parse_memory(result: CommandResult) -> MemoryInfo:
    """Decode a ``show memory`` response into :class:`MemoryInfo`."""
    rows: list[MemoryRow] = []
    page_stats: dict[str, str] = {}

    section_1018 = result.section(1018)
    if section_1018 is not None:
        for ln in section_1018.lines:
            text = ln.text
            stripped = text.strip()
            if not stripped:
                continue
            # Skip the column header line.
            if stripped.startswith("Effective") and "Overhead" in stripped:
                continue
            m = _LABELED_ROW_RE.match(stripped)
            if not m:
                continue
            label = m.group("label")
            rest = m.group("rest")
            # `rest` may contain 1 or 2 size values. Split by 2+ spaces
            # but be careful — sizes themselves can contain a single
            # space (e.g. "147.1 kB").
            parts = _split_size_pair(rest)
            if not parts:
                # Likely a single-value row like "Active pages: 144.0 kB"
                # but these are in a different section; skip here.
                continue
            if len(parts) == 1:
                rows.append(MemoryRow(label=label, effective=parts[0], overhead=""))
            else:
                rows.append(MemoryRow(label=label, effective=parts[0], overhead=parts[1]))

    # Page-stats rows live in a separate section (code 1028 in BIRD 3.x)
    # but historically they were continuation lines after the Total row.
    # We pick them up from any section that contains "Active pages:" /
    # "Kept free pages:" / "Cold free pages:".
    for sec in result.sections:
        for ln in sec.lines:
            text = ln.text.strip()
            for key in ("Active pages", "Kept free pages", "Cold free pages"):
                if text.startswith(key + ":"):
                    page_stats[key] = text[len(key) + 1 :].strip()

    return MemoryInfo(
        raw_text=result.raw_text,
        command=result.command,
        rows=rows,
        page_stats=page_stats,
    )


def _split_size_pair(rest: str) -> list[str]:
    """Split "147.1 kB     11.5 kB" into ["147.1 kB", "11.5 kB"].

    Sizes can be like "8351.0  B" (two spaces between number and unit).
    We match each size value via :data:`_SIZE_RE` (which tolerates the
    variable internal spacing) and return the original matched text.
    Returns 1 element if only one size is present, 2 if both are.
    """
    matches = list(_SIZE_RE.finditer(rest))
    return [m.group(0).strip() for m in matches]
