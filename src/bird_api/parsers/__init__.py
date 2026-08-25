"""Command-specific parsers for BIRD responses.

Each parser is a pure function ``parse(result: CommandResult) -> <Model>``.
Parsers never touch the network — they take a fully-decoded
:class:`~bird_api.protocol.CommandResult` and return a structured model
that also preserves the raw text.

Parsers raise :class:`~bird_api.exceptions.BirdParseError` (carrying the
raw text) when they cannot make sense of the response. Upper-layer code
can catch that, fall back to ``raw_text``, and continue — useful when a
new BIRD version introduces a field the parser doesn't yet know about.
"""

from __future__ import annotations

from .actions import parse_action
from .babel import parse_babel_interfaces, parse_babel_neighbors
from .bfd import parse_bfd_sessions
from .bgp import parse_bgp_session_detail, parse_bgp_sessions
from .config import parse_configure
from .eval import parse_eval
from .interfaces import parse_interfaces, parse_interfaces_summary
from .memory import parse_memory
from .mpls import parse_mpls_ranges
from .ospf import (
    parse_ospf,
    parse_ospf_interfaces,
    parse_ospf_lsadb,
    parse_ospf_neighbors,
    parse_ospf_state,
)
from .protocols import parse_protocols, parse_protocols_all
from .rip import parse_rip_interfaces, parse_rip_neighbors
from .route import parse_route
from .static import parse_static
from .status import parse_status
from .symbols import parse_symbols

__all__ = [
    "parse_status",
    "parse_protocols",
    "parse_protocols_all",
    "parse_route",
    "parse_memory",
    "parse_symbols",
    "parse_interfaces",
    "parse_interfaces_summary",
    "parse_eval",
    "parse_configure",
    "parse_action",
    "parse_bgp_sessions",
    "parse_bgp_session_detail",
    "parse_static",
    "parse_ospf",
    "parse_ospf_neighbors",
    "parse_ospf_interfaces",
    "parse_ospf_state",
    "parse_ospf_lsadb",
    "parse_bfd_sessions",
    "parse_rip_interfaces",
    "parse_rip_neighbors",
    "parse_babel_interfaces",
    "parse_babel_neighbors",
    "parse_mpls_ranges",
]
