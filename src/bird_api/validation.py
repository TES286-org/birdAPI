"""Input validation — bird_api's injection-prevention layer.

Threat model
------------
BIRD's control protocol is line-oriented: the daemon reads one ``\\n``-terminated
line at a time and executes each line as an **independent command**. There is no
quoting, escaping, or parameterisation at the protocol level — the client builds
command strings by interpolation. Consequently any user-controlled string that
reaches the wire containing a newline can smuggle additional commands::

    prefix = "10.0.0.0/8\\ndown"          # crafted input
    "show route " + prefix                # -> executes BOTH
                                          #    show route 10.0.0.0/8
                                          #    down   <-- daemon shutdown!

This was verified against a live BIRD 3.1.7 daemon: a single payload of
``"eval 1+2\\nshow status"`` produces two separate replies.

Defence strategy
----------------
Every value that :mod:`bird_api.client` interpolates into a command passes
through one of the validators in this module first. The checks are layered:

1. **Line safety** (the hard boundary): no control characters (C0, DEL) may
   appear anywhere — this alone makes newline/command injection impossible.
2. **Lexical safety**: each value class is restricted to the exact character
   set BIRD's lexer accepts for it (identifiers ``[A-Za-z_][A-Za-z0-9_]*``,
   printable-ASCII patterns without embedded quotes, canonical IP prefixes
   from :mod:`ipaddress`, …). This prevents *argument smuggling* (e.g. a
   protocol name of ``"x table y"`` re-shaping the command's parse) even
   within a single line.
3. **Structural safety**: enum-like options are matched against fixed sets,
   mutual exclusions are enforced, and total command length is capped at
   BIRD's ``CLI_RX_BUF_SIZE`` (4094 bytes) client-side.

All validators are pure functions returning the canonical string form;
they never perform I/O. Failures raise :class:`~bird_api.exceptions.BirdValidationError`.

Free-form escape hatches (``raw()``, ``eval()``, :class:`RouteFilter.raw`)
apply check (1) plus a length cap: they cannot *inject* commands, but they
intentionally allow the full BIRD filter language on a single line.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Final, NoReturn

from .exceptions import BirdValidationError
from .protocol import MAX_COMMAND_LENGTH

__all__ = [
    "MAX_IDENTIFIER_LENGTH",
    "MAX_PATTERN_LENGTH",
    "MAX_FILTER_LENGTH",
    "MAX_PATH_LENGTH",
    "READ_ONLY_COMMAND_RE",
    "is_read_only_command",
    "validate_command_line",
    "validate_identifier",
    "validate_pattern",
    "format_pattern",
    "validate_prefix",
    "validate_address",
    "validate_net_or_ip",
    "validate_choice",
    "validate_filter_expression",
    "validate_path",
    "validate_channel_arg",
    "validate_positive_int",
]

# --- limits -----------------------------------------------------------------

# BIRD's own symbol-name limit (conf/conf.h: SYM_MAX_LEN).
MAX_IDENTIFIER_LENGTH: Final[int] = 64
# Generous client-side caps for free-form-ish inputs; the real bound is the
# 4094-byte command buffer, but failing early with a clear message beats a
# cryptic daemon-side "command too long".
MAX_PATTERN_LENGTH: Final[int] = 256
MAX_FILTER_LENGTH: Final[int] = 2048
MAX_PATH_LENGTH: Final[int] = 1024

# --- compiled patterns -------------------------------------------------------

# BIRD lexer (conf/cf-lex.l): ALPHA [a-zA-Z_]  ALNUM [a-zA-Z_0-9]
_IDENTIFIER_RE: Final = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
# Any control character: C0 (0x00-0x1F) plus DEL (0x7F).
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")
# Printable ASCII, no quotes — used for shell-like protocol patterns.
_PATTERN_RE: Final = re.compile(r"\A[\x20-\x7e]+\Z")
# Printable ASCII — used for file paths (quotes excluded separately).
_PATH_RE: Final = re.compile(r"\A[\x20-\x7e]+\Z")
# A dotted BIRD channel reference: <protocol>.<channel> (nest/config.Y channel_arg).
_CHANNEL_ARG_RE: Final = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\Z")

# Read-only command allowlist shared by the sync and async connections
# (and the test fakes). Only inspection commands pass; anything that mutates
# daemon state (configure / enable / disable / restart / reload / dump / down /
# ...) or session state is rejected client-side, before a single byte is
# written to the socket. This is deliberately conservative: ``eval`` and
# ``echo`` are excluded too (they are not needed for monitoring and ``eval``
# can burn daemon CPU on pathological expressions). ``configure status`` is
# read-only introspection, and ``restrict`` only ever *tightens* the
# session's own permissions, so both are allowed.
READ_ONLY_COMMAND_RE: Final[re.Pattern[str]] = re.compile(
    r"\A(?:show\b|configure\s+status\b|restrict\b)",
)


def is_read_only_command(command: str) -> bool:
    """Return ``True`` when ``command`` is allowed on a ``read_only`` connection."""
    return READ_ONLY_COMMAND_RE.match(command) is not None


# --- internal helpers --------------------------------------------------------


def _fail(field: str, value: object, reason: str) -> NoReturn:
    raise BirdValidationError(
        f"invalid {field} {value!r}: {reason}",
        field=field,
        value=value,
    )


def _check_no_control(value: str, field: str) -> None:
    """Reject any string containing a control character.

    This is the non-negotiable line-framing safety check: a newline would
    terminate the command early and let everything after it execute as a
    fresh command; other control characters are never meaningful in BIRD
    input and are rejected defensively.
    """
    if _CONTROL_RE.search(value):
        _fail(field, value, "contains control characters (possible command-injection attempt)")


def _check_str(value: object, field: str, *, max_length: int) -> str:
    """Coerce to ``str`` (accepting str subclasses) and enforce basic sanity."""
    if not isinstance(value, str):
        _fail(field, value, f"expected a string, got {type(value).__name__}")
    if not value:
        _fail(field, value, "must not be empty")
    if len(value) > max_length:
        _fail(field, value, f"longer than {max_length} characters")
    return value


# --- public validators --------------------------------------------------------


def validate_command_line(command: object, *, field: str = "command") -> str:
    """Validate a full raw command line (used by :meth:`BirdClient.raw`).

    Rules: must be a non-empty string, contain no control characters, and
    fit in BIRD's command buffer. This keeps the line-framing invariant
    intact while intentionally allowing the full command language.
    """
    command = _check_str(command, field, max_length=MAX_COMMAND_LENGTH)
    _check_no_control(command, field)
    return command


def validate_identifier(value: object, *, field: str = "identifier") -> str:
    """Validate a BIRD symbol name (protocol, table, filter, channel, …).

    Accepts exactly the lexer's word rule ``[A-Za-z_][A-Za-z0-9_]*`` and a
    maximum length of ``SYM_MAX_LEN`` (64). Anything else — spaces, quotes,
    wildcards, newlines, non-ASCII — is rejected.
    """
    value = _check_str(value, field, max_length=MAX_IDENTIFIER_LENGTH)
    _check_no_control(value, field)  # clearer error than a bare regex mismatch
    if not _IDENTIFIER_RE.match(value):
        _fail(
            field,
            value,
            "must match [A-Za-z_][A-Za-z0-9_]* (BIRD symbol-name rule)",
        )
    return value


def validate_pattern(value: object, *, field: str = "pattern") -> str:
    """Validate a shell-like protocol pattern (e.g. ``bgp*``, ``ospf?``).

    BIRD matches these with :code:`patmatch()` which understands ``*`` and
    ``?``. The pattern must be printable ASCII without double quotes (the
    wire form is a quoted string and BIRD's lexer has no escape for an
    embedded quote).
    """
    value = _check_str(value, field, max_length=MAX_PATTERN_LENGTH)
    if not _PATTERN_RE.match(value):
        _fail(field, value, "must be printable ASCII without double quotes")
    if '"' in value:
        _fail(field, value, "must not contain double quotes (would break out of the quoted pattern)")
    _check_no_control(value, field)  # redundant with ASCII check, kept explicit
    return value


def format_pattern(value: object, *, field: str = "pattern") -> str:
    """Validate a protocol pattern and return its wire form.

    Bare identifiers and the keyword ``all`` are emitted unquoted (they are
    single tokens); everything else is double-quoted, which is the only way
    BIRD's grammar accepts wildcard patterns (``proto_patt``: ``TEXT``).
    """
    value = validate_pattern(value, field=field)
    if value == "all" or _IDENTIFIER_RE.match(value):
        return value
    return f'"{value}"'


def validate_prefix(value: object, *, field: str = "prefix") -> str:
    """Validate an IP network prefix; returns the canonical string.

    Accepts :class:`ipaddress.IPv4Network` / :class:`ipaddress.IPv6Network`
    objects (canonicalised) or strings. A bare IP address is accepted as a
    host route (``192.0.2.1`` → ``192.0.2.1/32``). Prefixes with host bits
    set are rejected — BIRD itself rejects them (``Invalid IPv4 prefix …,
    maybe you wanted …``) and silently masking them would change query
    semantics.
    """
    if isinstance(value, ipaddress.IPv4Network | ipaddress.IPv6Network):
        return str(value)
    if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return f"{value}/{value.max_prefixlen}"
    if not isinstance(value, str):
        _fail(field, value, f"expected str or ipaddress network, got {type(value).__name__}")
    assert isinstance(value, str)  # for the type checker
    if len(value) > 64:
        _fail(field, value, "too long")
    _check_no_control(value, field)
    stripped = value.strip()
    if not stripped:
        _fail(field, value, "must not be empty")
    try:
        # Bare address -> host route
        if "/" not in stripped:
            addr = ipaddress.ip_address(stripped)
            return f"{addr}/{addr.max_prefixlen}"
        # strict=True rejects host bits, mirroring BIRD's own behaviour.
        return str(ipaddress.ip_network(stripped, strict=True))
    except ValueError as exc:
        _fail(field, value, f"not a valid IP prefix ({exc})")
        raise  # unreachable; placate type checkers


def validate_address(value: object, *, field: str = "address") -> str:
    """Validate a plain IP address (no prefix) for ``show route for <ip>``.

    Accepts :class:`ipaddress.IPv4Address` / :class:`ipaddress.IPv6Address`
    or a string. A ``a.b.c.d/len`` form is rejected — use
    :func:`validate_prefix` for prefix queries.
    """
    if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return str(value)
    if not isinstance(value, str):
        _fail(field, value, f"expected str or ipaddress address, got {type(value).__name__}")
    assert isinstance(value, str)
    if len(value) > 64:
        _fail(field, value, "too long")
    _check_no_control(value, field)
    stripped = value.strip()
    try:
        return str(ipaddress.ip_address(stripped))
    except ValueError as exc:
        _fail(field, value, f"not a valid IP address ({exc})")
        raise  # unreachable; placate type checkers


def validate_net_or_ip(value: object, *, field: str = "address or prefix") -> str:
    """Validate an argument accepted by BIRD's ``net_or_ipa`` rule.

    Accepts a bare IP address (kept bare — exact-address semantics) or an
    IP prefix (canonicalised, host bits rejected). Used by
    ``show bfd sessions address …``.
    """
    if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return str(value)
    if isinstance(value, ipaddress.IPv4Network | ipaddress.IPv6Network):
        return str(value)
    if not isinstance(value, str):
        _fail(field, value, f"expected str or ipaddress value, got {type(value).__name__}")
    assert isinstance(value, str)
    if len(value) > 64:
        _fail(field, value, "too long")
    _check_no_control(value, field)
    stripped = value.strip()
    if not stripped:
        _fail(field, value, "must not be empty")
    if "/" not in stripped:
        try:
            return str(ipaddress.ip_address(stripped))
        except ValueError as exc:
            _fail(field, value, f"not a valid IP address ({exc})")
    try:
        return str(ipaddress.ip_network(stripped, strict=True))
    except ValueError as exc:
        _fail(field, value, f"not a valid IP prefix ({exc})")
    raise  # unreachable; placate type checkers


def validate_choice(value: object, allowed: Iterable[str], *, field: str = "option") -> str:
    """Validate that ``value`` is one of the fixed strings in ``allowed``."""
    allowed_set = allowed if isinstance(allowed, frozenset) else frozenset(allowed)
    if not isinstance(value, str) or value not in allowed_set:
        _fail(field, value, f"must be one of {sorted(allowed_set)!r}")
    return value


def validate_filter_expression(value: object, *, field: str = "filter expression") -> str:
    """Validate a free-form BIRD filter expression (used by ``where`` / ``eval``).

    Security contract: the expression must be a single line of printable
    ASCII with no control characters — this preserves line framing, so no
    additional commands can be smuggled. Double quotes ARE allowed because
    the filter language needs them (``proto = "bgp1"``); an unbalanced quote
    can at worst cause a daemon-side syntax error (9001), never injection.

    Prefer :class:`~bird_api.queries.RouteFilter`, which builds expressions
    from individually validated parts, for anything caller-facing.
    """
    value = _check_str(value, field, max_length=MAX_FILTER_LENGTH)
    if not value.strip():
        _fail(field, value, "must not be blank")
    _check_no_control(value, field)
    # Printable ASCII, double quotes allowed (filter string literals).
    if not re.fullmatch(r"[\x20-\x7e]+", value):
        _fail(field, value, "must be a single line of printable ASCII")
    return value


def validate_path(value: object, *, field: str = "path") -> str:
    """Validate a file path for ``configure`` / ``dump`` commands.

    The wire form is a double-quoted string and BIRD's lexer provides no
    escape for embedded quotes, so double quotes are rejected outright.
    Everything else printable-ASCII is allowed (spaces included).
    """
    value = _check_str(value, field, max_length=MAX_PATH_LENGTH)
    if not _PATH_RE.match(value):
        _fail(field, value, "must be printable ASCII")
    if '"' in value:
        _fail(field, value, "must not contain double quotes (BIRD cannot escape them)")
    _check_no_control(value, field)
    return value


def validate_channel_arg(value: object, *, field: str = "channel reference") -> str:
    """Validate a ``<protocol>`` or ``<protocol>.<channel>`` reference.

    Used by ``show route (import|export) table <p>[.<c>]`` and the export
    mode family.
    """
    if not isinstance(value, str):
        _fail(field, value, f"expected a string, got {type(value).__name__}")
    if "." in value:
        if not _CHANNEL_ARG_RE.match(value):
            _fail(field, value, "must be '<protocol>.<channel>' with BIRD symbol names")
        # Validate each half explicitly for better error messages.
        proto, _chan = value.split(".", 1)
        validate_identifier(proto, field=f"{field} (protocol part)")
        return value
    return validate_identifier(value, field=field)


def validate_positive_int(value: object, *, field: str = "value", maximum: int | None = None) -> int:
    """Validate an integer option (e.g. configure timeout, AS numbers)."""
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(field, value, f"expected an int, got {type(value).__name__}")
    if value < 0:
        _fail(field, value, "must be non-negative")
    if maximum is not None and value > maximum:
        _fail(field, value, f"must be <= {maximum}")
    return value


def check_command_length(command: str) -> str:
    """Final guard: ensure the assembled command fits BIRD's input buffer."""
    if len(command) > MAX_COMMAND_LENGTH:
        raise BirdValidationError(
            f"assembled command is {len(command)} bytes, exceeding BIRD's "
            f"{MAX_COMMAND_LENGTH}-byte input buffer",
            field="command",
            value=command[:64] + "…",
        )
    return command
