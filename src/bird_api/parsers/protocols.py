"""Parser for ``show protocols`` and ``show protocols all``.

Wire example (``show protocols``)::

    2002-Name       Proto      Table      State  Since         Info
    1002-static_test Static     master4    up     13:22:41.388
     static_test2 Static     master4    up     13:22:41.388
     device1    Device     ---        up     13:22:41.388
     kernel1    Kernel     master4    up     13:22:41.388
     direct1    Direct     ---        up     13:22:41.388
    0000

Field layout (``nest/proto.c:2181, 2188``):
    %-10s %-10s %-10s %-6s %-12s  %s
    name      proto     table    state since        info

Each field is space-padded to its width and separated by a SINGLE space.
When a value exceeds its column width (e.g. ``static_test`` is 11 chars
but the column is 10), the separator collapses to a single space. We
therefore use a single regex with greedy non-space groups and 1+ space
separators to robustly extract the five columns plus optional info.

``show protocols all`` interleaves the summary section (code 1002, one
row per protocol) with verbose ``1006-`` blocks carrying description /
channel info. Each protocol's verbose block ends with a blank ``1006-``
separator line. The parser walks sections in order and pairs each
summary row with the following 1006 block.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import ChannelState, ProtocolDetail, ProtocolRow, ProtocolsAllResult, ProtocolsResult
from ..protocol import CommandResult

__all__ = ["parse_protocols", "parse_protocols_all", "parse_protocol_row"]

# Single regex for the 5 fixed columns + optional info. We use \S+ (greedy,
# non-space) for each column and \s+ between. The trailing 2 spaces before
# the info column (per the format string) are absorbed into the \s* match.
_ROW_RE = re.compile(
    r"^(?P<name>\S+)\s+(?P<proto>\S+)\s+(?P<table>\S+)\s+(?P<state>\S+)"
    r"\s+(?P<since>\S+)\s*(?P<info>.*)$",
)


def parse_protocol_row(line: str) -> ProtocolRow:
    """Parse a single ``show protocols`` summary row into a :class:`ProtocolRow`.

    The line is expected to have its protocol prefix (``NNNN-`` or `` ``)
    already stripped; this function only sees the field text.
    """
    line = line.rstrip()
    m = _ROW_RE.match(line)
    if not m:
        # Unparseable row — return an empty placeholder so callers still get
        # something usable. The ``raw_text`` of the parent result preserves
        # the original line for inspection.
        return ProtocolRow(name=line.strip(), protocol="", table="", state="", since="", info="")
    return ProtocolRow(
        name=m.group("name").strip(),
        protocol=m.group("proto").strip(),
        table=m.group("table").strip(),
        state=m.group("state").strip(),
        since=m.group("since").strip(),
        info=m.group("info").strip(),
    )


def parse_protocols(result: CommandResult) -> ProtocolsResult:
    """Decode a ``show protocols`` response into :class:`ProtocolsResult`."""
    rows: list[ProtocolRow] = []
    section_1002 = result.section(1002)
    if section_1002 is None:
        return ProtocolsResult(
            raw_text=result.raw_text,
            command=result.command,
            rows=[],
        )
    for ln in section_1002.lines:
        text = ln.text
        if not text.strip():
            continue
        rows.append(parse_protocol_row(text))
    return ProtocolsResult(
        raw_text=result.raw_text,
        command=result.command,
        rows=rows,
    )


def parse_protocols_all(result: CommandResult) -> ProtocolsAllResult:
    """Decode a ``show protocols all`` response into :class:`ProtocolsAllResult`.

    The response alternates between summary rows (code 1002, one row each)
    and verbose blocks (code 1006, multiple lines per protocol). The parser
    walks sections in arrival order, pairs each 1002 row with the next 1006
    block, and decodes the channel info inside the 1006 block.
    """
    rows: list[ProtocolRow] = []
    details: list[ProtocolDetail] = []

    sections = result.sections
    i = 0
    while i < len(sections):
        sec = sections[i]
        if sec.code != 1002:
            i += 1
            continue

        # The 1002 section's first non-empty line is the summary row.
        row_lines = [ln.text for ln in sec.lines if ln.text.strip()]
        if not row_lines:
            i += 1
            continue
        row = parse_protocol_row(row_lines[0])
        rows.append(row)

        # Collect all 1006 blocks that follow this 1002 row.
        block_lines: list[str] = []
        j = i + 1
        while j < len(sections) and sections[j].code == 1006:
            block_lines.extend(ln.text for ln in sections[j].lines)
            j += 1

        detail = _parse_detail_block(block_lines, row)
        details.append(detail)
        i = j

    return ProtocolsAllResult(
        raw_text=result.raw_text,
        command=result.command,
        rows=rows,
        details=details,
    )


def _parse_detail_block(lines: list[str], row: ProtocolRow) -> ProtocolDetail:
    """Parse a single protocol's verbose block (all 1006 lines concatenated)."""
    created: str | None = None
    description: str | None = None
    message: str | None = None
    router_id: str | None = None
    vrf: str | None = None
    channels: list[ChannelState] = []
    detail_lines: list[str] = []

    # Build a "channel-in-progress" using a mutable dict, then freeze it
    # into a ChannelState once we move past it.
    current: dict[str, Any] | None = None
    current_extra: list[str] = []
    current_stats: dict[str, dict[str, str]] = {}

    def _flush_current() -> None:
        nonlocal current, current_extra, current_stats
        if current is None:
            return
        channels.append(
            ChannelState(
                name=current.get("name", ""),
                state=current.get("state", ""),
                table=current.get("table", ""),
                preference=current.get("preference", ""),
                input_filter=current.get("input_filter", ""),
                output_filter=current.get("output_filter", ""),
                routes_imported=current.get("routes_imported"),
                routes_filtered=current.get("routes_filtered"),
                routes_exported=current.get("routes_exported"),
                routes_preferred=current.get("routes_preferred"),
                route_change_stats=dict(current_stats),
                extra_lines=list(current_extra),
            )
        )
        current = None
        current_extra = []
        current_stats = {}

    for raw_line in lines:
        text = raw_line.lstrip()
        if not text:
            continue
        if text.startswith("Channel "):
            _flush_current()
            current = {"name": text[len("Channel ") :].strip()}
            current_extra = []
            current_stats = {}
            continue
        if current is not None:
            _apply_channel_line(current, current_stats, current_extra, text)
            continue
        # Top-level protocol detail fields.
        if text.startswith("Created:"):
            created = text[len("Created:") :].strip()
        elif text.startswith("Description:"):
            description = text[len("Description:") :].strip()
        elif text.startswith("Message:"):
            message = text[len("Message:") :].strip()
        elif text.startswith("Router ID:"):
            router_id = text[len("Router ID:") :].strip()
        elif text.startswith("VRF:"):
            vrf = text[len("VRF:") :].strip()
        else:
            detail_lines.append(text)

    _flush_current()

    return ProtocolDetail(
        name=row.name,
        protocol=row.protocol,
        table=row.table,
        state=row.state,
        since=row.since,
        info=row.info,
        created=created,
        description=description,
        message=message,
        router_id=router_id,
        vrf=vrf,
        channels=channels,
        detail_lines=detail_lines,
    )


_ROUTES_RE = re.compile(
    r"Routes:\s+(\d+)\s+imported"
    r"(?:,\s+(\d+)\s+filtered)?"
    r",\s+(\d+)\s+exported"
    r",\s+(\d+)\s+preferred",
)


def _apply_channel_line(
    current: dict[str, Any],
    stats: dict[str, dict[str, str]],
    extra: list[str],
    text: str,
) -> None:
    """Update the in-progress channel dict with one ``<label>: <value>`` line."""
    if text.startswith("State:"):
        current["state"] = text[len("State:") :].strip()
    elif text.startswith("Table:"):
        current["table"] = text[len("Table:") :].strip()
    elif text.startswith("Preference:"):
        current["preference"] = text[len("Preference:") :].strip()
    elif text.startswith("Input filter:"):
        current["input_filter"] = text[len("Input filter:") :].strip()
    elif text.startswith("Output filter:"):
        current["output_filter"] = text[len("Output filter:") :].strip()
    elif text.startswith("Routes:"):
        m = _ROUTES_RE.search(text)
        if m:
            current["routes_imported"] = int(m.group(1))
            current["routes_filtered"] = int(m.group(2)) if m.group(2) else None
            current["routes_exported"] = int(m.group(3))
            current["routes_preferred"] = int(m.group(4))
    elif text.startswith("Route change stats:"):
        # Header row: "received   rejected   filtered    ignored   RX limit      limit   accepted"
        # BIRD uses fixed-width columns here; ``RX limit`` is a single
        # column header that contains a space. ``.split()`` would break it
        # into ``RX`` and ``limit``. Merge them back if that happened.
        headers = text[len("Route change stats:") :].split()
        # Merge "RX" + "limit" into "RX limit" (the only multi-word header).
        for i in range(len(headers) - 1):
            if headers[i] == "RX" and headers[i + 1] == "limit":
                headers[i] = "RX limit"
                del headers[i + 1]
                break
        # Stash headers on the current dict; consumed by the rows that follow.
        current["_stats_headers"] = headers
    elif text.startswith("Import updates:") or text.startswith("Import withdraws:") or text.startswith(
        "Export updates:",
    ) or text.startswith("Export withdraws:"):
        label, _, rest = text.partition(":")
        values = rest.split()
        # Use .get() (not .pop()) so subsequent rows in the same channel
        # block can also pair against the headers.
        stashed_headers: list[str] | None = current.get("_stats_headers")
        if stashed_headers:
            # Pair each header with each value; some values may be "---".
            pairs = dict(zip(stashed_headers, values, strict=False))
            stats[label.strip()] = pairs
        else:
            extra.append(text)
    else:
        extra.append(text)


# Re-export the helper for callers that want to parse a single line.
# (Back-compat alias from the previous API.)
def parse_protocol_summary(line: str) -> ProtocolRow:
    """Alias for :func:`parse_protocol_row`."""
    return parse_protocol_row(line)
