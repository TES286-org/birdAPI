"""Parser for BGP session details from ``show protocols all`` output.

Consumes the :class:`~bird_api.models.ProtocolDetail` blocks produced by
:func:`~bird_api.parsers.parse_protocols_all` for protocols of type
``BGP`` and extracts the structured fields emitted by
``bgp_show_proto_info()`` (proto/bgp/bgp.c), e.g.::

    BGP state:          Established
      Neighbor address: 127.0.0.3
      Neighbor port:    1179
      Neighbor AS:      65000
      Local AS:         65000
      Neighbor ID:      10.0.0.2
      Local capabilities
        Multiprotocol
          AF announced: ipv4
      Neighbor capabilities
        Multiprotocol
      Session:          internal multihop AS4
      Source address:   127.0.0.2
      Hold timer:       175.586/240
      Keepalive timer:  63.888/80
      Send hold timer:  370.520/480

The protocols parser strips leading whitespace from detail lines, so
capability sub-blocks are detected by marker lines ("Local capabilities" /
"Neighbor capabilities") terminated by the next top-level key rather than
by indentation. Lines the parser does not model explicitly are preserved
verbatim in ``BgpSession.extra_lines``.
"""

from __future__ import annotations

import re

from ..models import BgpSession, BgpSessionsResult, ProtocolDetail

__all__ = ["parse_bgp_sessions", "parse_bgp_session_detail"]

# Top-level keys emitted by bgp_show_proto_info(). A line starting with one
# of these (followed by ':' or being the whole line) closes an open
# capability sub-block.
_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "BGP state",
    "Neighbor address",
    "Neighbor range",
    "Neighbor port",
    "Neighbor AS",
    "Local AS",
    "Neighbor ID",
    "Neighbor graceful restart active",
    "Error wait",
    "Connect delay",
    "Restart timer",
    "Session",
    "Source address",
    "Hold timer",
    "Keepalive timer",
    "Send hold timer",
    "TX pending",
    "Last error",
    "TCP-AO",
)

_CAPABILITY_MARKERS = frozenset({"Local capabilities", "Neighbor capabilities"})

# "Key: value" and "Key:" lines (keys may contain spaces).
_KEY_VALUE_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9 _-]*?):\s+(?P<value>\S.*)$")
_KEY_ONLY_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9 _-]*?):\s*$")


def _is_top_level(line: str) -> bool:
    return any(line == key or line.startswith(key + ":") for key in _TOP_LEVEL_KEYS)


def parse_bgp_session_detail(detail: ProtocolDetail) -> BgpSession:
    """Parse one BGP protocol's detail block into a :class:`BgpSession`."""
    capabilities: list[str] = []
    extra_lines: list[str] = []
    fields: dict[str, str] = {}

    in_capabilities = False
    for raw in detail.detail_lines:
        line = raw.strip()
        if not line:
            continue

        if line in _CAPABILITY_MARKERS:
            in_capabilities = True
            capabilities.append(line)
            continue

        if in_capabilities and not _is_top_level(line):
            capabilities.append(line)
            continue
        in_capabilities = False

        if _is_top_level(line):
            m = _KEY_VALUE_RE.match(line)
            if m:
                fields[m.group("key")] = m.group("value").strip()
                continue
            m2 = _KEY_ONLY_RE.match(line)
            if m2:
                fields[m2.group("key")] = ""
                continue
            # A colon-less top-level line (e.g. "Neighbor graceful restart active").
            fields[line] = ""
            continue

        extra_lines.append(line)

    def _int(key: str) -> int | None:
        value = fields.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    return BgpSession(
        name=detail.name,
        state=fields.get("BGP state", ""),
        neighbor_address=fields.get("Neighbor address"),
        neighbor_port=_int("Neighbor port"),
        neighbor_as=_int("Neighbor AS"),
        local_as=_int("Local AS"),
        neighbor_id=fields.get("Neighbor ID"),
        session=fields.get("Session"),
        source_address=fields.get("Source address"),
        hold_timer=fields.get("Hold timer"),
        keepalive_timer=fields.get("Keepalive timer"),
        send_hold_timer=fields.get("Send hold timer"),
        last_error=fields.get("Last error"),
        capabilities=capabilities,
        extra_lines=extra_lines,
    )


def parse_bgp_sessions(
    details: list[ProtocolDetail],
    *,
    raw_text: str,
    command: str,
) -> BgpSessionsResult:
    """Filter protocol details to BGP and parse each one into a session."""
    sessions = [parse_bgp_session_detail(d) for d in details if d.protocol.upper() == "BGP"]
    return BgpSessionsResult(raw_text=raw_text, command=command, sessions=sessions)
