"""Parser for ``show bfd sessions`` (code 1020).

Two output forms (proto/bfd/bfd.c ``bfd_show_sessions``):

Table form (default)::

    1020-bfd_test:
     IP address                Interface  State      Since         Interval  Timeout
     127.0.0.3                 ---        Up         05:32:42.734    0.100    0.500
    0000

Verbose form (``all`` option) — one indented block per session::

    1020-bfd_test:
       Address:              127.0.0.3
       Interface:            ---
       Session type:         Multihop
       Session state:        Up
       Remote state:         Up
       Last state change:    05:32:42.734
       Local diagnostic:     None
       Remote diagnostic:    None
       Local discriminator:  2370786090
       Remote discriminator: 683441413
       Transmit timer:       0.001 / 0.100
       Detect timer:         0.467 / 0.500
       Local parameters:
         Min TX interval:    0.100
         Min RX interval:    0.010
         Demand mode:        No
         Multiplier:         5
       Remote parameters:
         ...
    0000

The command walks every BFD protocol (``PROTO_WALK_CMD``), so multiple
protocol headers may appear; ``protocols`` lists them in order and rows /
details follow each header.
"""

from __future__ import annotations

import re

from ..models import BfdSessionDetail, BfdSessionRow, BfdSessionsResult
from ..protocol import CommandResult

__all__ = ["parse_bfd_sessions"]

_NOT_UP_RE = re.compile(r"^(?P<name>\S+):\s+is not up\s*$")
_PROTOCOL_HEADER_RE = re.compile(r"^(?P<name>\S+):\s*$")
_COLUMN_HEADER_RE = re.compile(r"^IP address\s+Interface\s+State\s+Since\s+Interval\s+Timeout\s*$")

# "%-25I %-10s %-10s %-12s  %7t  %7t"
_ROW_RE = re.compile(
    r"^(?P<addr>\S+)\s+(?P<iface>\S+)\s+(?P<state>\S+)\s+(?P<since>\S+)\s+"
    r"(?P<interval>\S+)\s+(?P<timeout>\S+)\s*$",
)

_KV_RE = re.compile(r"^(?P<key>[A-Za-z0-9 ]+?):\s+(?P<value>\S.*)$")
_KV_ONLY_RE = re.compile(r"^(?P<key>[A-Za-z0-9 ]+?):\s*$")


def parse_bfd_sessions(result: CommandResult) -> BfdSessionsResult:
    """Decode a ``show bfd sessions`` response (table or verbose form)."""
    section = result.section(1020)
    protocols: list[str] = []
    rows: list[BfdSessionRow] = []
    details: list[BfdSessionDetail] = []
    up = True

    if section is None:
        return BfdSessionsResult(
            raw_text=result.raw_text,
            command=result.command,
            protocols=[],
            up=True,
            rows=[],
            details=[],
        )

    # Verbose state machine.
    detail: dict[str, str] = {}
    local_params: dict[str, str] = {}
    remote_params: dict[str, str] = {}
    extra: list[str] = []
    in_detail = False
    param_block: dict[str, str] | None = None

    def _flush_detail() -> None:
        nonlocal detail, local_params, remote_params, extra, in_detail, param_block
        if in_detail and detail:
            details.append(
                BfdSessionDetail(
                    address=detail.get("Address"),
                    interface=detail.get("Interface"),
                    session_type=detail.get("Session type"),
                    state=detail.get("Session state"),
                    remote_state=detail.get("Remote state"),
                    fields=dict(detail),
                    local_params=dict(local_params),
                    remote_params=dict(remote_params),
                    extra_lines=list(extra),
                ),
            )
        detail = {}
        local_params = {}
        remote_params = {}
        extra = []
        in_detail = False
        param_block = None

    for ln in section.lines:
        text = ln.text
        stripped = text.strip()
        if not stripped:
            continue

        m = _NOT_UP_RE.match(stripped)
        if m:
            protocols.append(m.group("name"))
            up = False
            continue
        m = _PROTOCOL_HEADER_RE.match(stripped)
        if m and not _COLUMN_HEADER_RE.match(stripped):
            protocols.append(m.group("name"))
            continue
        if _COLUMN_HEADER_RE.match(stripped):
            continue

        # Verbose session block lines start with two+ spaces on the wire
        # (after the continuation prefix, at least one leading space).
        verbose_line = text.startswith(" ")

        if verbose_line:
            inner = stripped
            if inner.startswith(("Local parameters", "Remote parameters")):
                param_block = local_params if inner.startswith("Local") else remote_params
                continue
            m = _KV_RE.match(inner)
            if m:
                key = m.group("key").strip()
                if key == "Address":
                    # A new session block begins.
                    _flush_detail()
                    in_detail = True
                target = param_block if param_block is not None else detail
                target[key] = m.group("value").strip()
                continue
            if _KV_ONLY_RE.match(inner):
                target = param_block if param_block is not None else detail
                target[_KV_ONLY_RE.match(inner).group("key").strip()] = ""  # type: ignore[union-attr]
                continue
            if param_block is not None:
                param_block = None  # left the parameter block
            extra.append(inner)
            continue

        # Table row (no leading space).
        _flush_detail()
        m = _ROW_RE.match(stripped)
        if m:
            rows.append(
                BfdSessionRow(
                    address=m.group("addr"),
                    interface=m.group("iface"),
                    state=m.group("state"),
                    since=m.group("since"),
                    interval=m.group("interval"),
                    timeout=m.group("timeout"),
                ),
            )

    _flush_detail()

    return BfdSessionsResult(
        raw_text=result.raw_text,
        command=result.command,
        protocols=protocols,
        up=up,
        rows=rows,
        details=details,
    )
