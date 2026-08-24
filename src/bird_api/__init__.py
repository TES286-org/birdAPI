"""bird-api — Python library for talking to the BIRD Internet Routing Daemon.

Public surface:

* :class:`BirdClient` — the main entry point. Owns a persistent Unix
  socket and exposes one method per supported BIRD command family.
* :class:`BirdConnection` — lower-level connection you can use directly
  if you only want raw :class:`CommandResult` objects without command
  wrappers.
* :class:`CommandResult` and related :class:`ReplyLine` /
  :class:`ReplySection` — fully-decoded wire response, including the
  ``raw_text`` reconstruction for upper-layer logging.
* The structured data classes in :mod:`bird_api.models` (``Status``,
  ``ProtocolRow``, ``RouteEntry``, etc.) — typed views on top of the
  decoded response.
* :class:`BirdError` and its subclasses — all exceptions raised by the
  library.

Example
-------
>>> from bird_api import BirdClient
>>> with BirdClient(socket_path="/tmp/bird.ctl") as client:
...     status = client.show_status()
...     print(status.bird_version, status.router_id)
...     for row in client.show_protocols().rows:
...         print(row.name, row.state)

See ``README.md`` for installation instructions and the design notes in
``scripts/bird-protocol-research.md`` for protocol details.
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
)
from .models import (
    ActionResult,
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

__version__ = "0.1.0"

__all__ = [
    # Client
    "BirdClient",
    "BirdConnection",
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
    "__version__",
]
