"""bird-api — Python library for talking to the BIRD Internet Routing Daemon.

Public surface:

* :class:`BirdClient` — the main entry point. Owns a persistent Unix
  socket and exposes one method per supported BIRD command family.
  Supports a ``read_only`` mode that blocks mutating commands client-side.
* :class:`RouteQuery` / :class:`RouteFilter` — typed, **validated** builders
  for the full ``show route`` grammar and ``where`` filter expressions.
* :class:`BirdConnection` — lower-level connection you can use directly
  if you only want raw :class:`CommandResult` objects without command
  wrappers.
* :class:`CommandResult` and related :class:`ReplyLine` /
  :class:`ReplySection` — fully-decoded wire response, including the
  ``raw_text`` reconstruction for upper-layer logging.
* The structured data classes in :mod:`bird_api.models` (``Status``,
  ``ProtocolRow``, ``RouteEntry``, ``BgpSession``, etc.) — typed views on
  top of the decoded response.
* :class:`BirdError` and its subclasses — all exceptions raised by the
  library. :class:`BirdValidationError` is raised when caller input fails
  the injection-safety checks in :mod:`bird_api.validation`.

Example
-------
>>> from bird_api import BirdClient, RouteFilter, RouteQuery
>>> with BirdClient(socket_path="/tmp/bird.ctl") as client:
...     status = client.show_status()
...     print(status.bird_version, status.router_id)
...     for row in client.show_protocols().rows:
...         print(row.name, row.state)
...     q = RouteQuery().table("master4").where(RouteFilter.protocol("static_test"))
...     for entry in client.show_route_query(q).entries:
...         print(entry.prefix, entry.preference)

See ``README.md`` for installation instructions and the design notes in
``docs/bird-protocol-research.md`` for protocol details.
"""

from __future__ import annotations

from .client import BirdClient
from .connection import DEFAULT_SOCKET_PATH, DEFAULT_TIMEOUT, BirdConnection
from .exceptions import (
    BirdCommandError,
    BirdConnectionError,
    BirdError,
    BirdParseError,
    BirdProtocolError,
    BirdTimeoutError,
    BirdValidationError,
)
from .models import (
    ActionResult,
    BgpSession,
    BgpSessionsResult,
    ChannelState,
    ConfigResult,
    EvalResult,
    InterfaceAddress,
    InterfaceOne,
    InterfacesResult,
    InterfacesSummaryResult,
    InterfaceSummaryRow,
    MemoryInfo,
    MemoryRow,
    ParsedResponse,
    ProtocolDetail,
    ProtocolRow,
    ProtocolsAllResult,
    ProtocolsResult,
    RouteEntry,
    RouteResult,
    Status,
    Symbol,
    SymbolsResult,
)
from .protocol import (
    CommandResult,
    ReplyLine,
    ReplyLineKind,
    ReplySection,
    assemble_result,
    parse_line,
)
from .queries import RouteFilter, RouteQuery

__version__ = "0.2.0"

__all__ = [
    # Client
    "BirdClient",
    "BirdConnection",
    # Complex query builders
    "RouteQuery",
    "RouteFilter",
    # Constants
    "DEFAULT_SOCKET_PATH",
    "DEFAULT_TIMEOUT",
    # Models
    "ParsedResponse",
    "Status",
    "ProtocolsResult",
    "ProtocolRow",
    "ProtocolsAllResult",
    "ProtocolDetail",
    "ChannelState",
    "RouteResult",
    "RouteEntry",
    "MemoryInfo",
    "MemoryRow",
    "SymbolsResult",
    "Symbol",
    "InterfacesResult",
    "InterfaceOne",
    "InterfaceAddress",
    "InterfacesSummaryResult",
    "InterfaceSummaryRow",
    "EvalResult",
    "ConfigResult",
    "ActionResult",
    "BgpSession",
    "BgpSessionsResult",
    # Protocol
    "CommandResult",
    "ReplyLine",
    "ReplyLineKind",
    "ReplySection",
    "parse_line",
    "assemble_result",
    # Exceptions
    "BirdError",
    "BirdConnectionError",
    "BirdProtocolError",
    "BirdCommandError",
    "BirdTimeoutError",
    "BirdParseError",
    "BirdValidationError",
    "__version__",
]
