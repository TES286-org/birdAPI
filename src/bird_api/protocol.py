"""Wire protocol parsing for the BIRD control socket.

This module knows nothing about sockets — it only turns bytes into
:class:`ReplyLine` objects and groups them into :class:`CommandResult`.
It is deliberately separated from :mod:`bird_api.connection` so it can
be unit-tested with pure string fixtures.

Wire format summary (see ``scripts/bird-protocol-research.md`` for details):

* The protocol is **line-oriented**, framed by ``\\n`` (LF).
* Every reply line begins with one of four prefixes:

  ==============  ===================  ====================  ==================================
  Wire prefix     ``ReplyLine.kind``  ``ReplyLine.code``    Meaning
  ==============  ===================  ====================  ==================================
  ``+<text>``     ``ASYNC``           ``None``               Async / unsolicited message
  `` <text>``     ``CONTINUATION``    ``None``               Same-code continuation (text after the space)
  ``NNNN-<text>`` ``SECTION_START``   ``NNNN``               First line of a new code section
  ``NNNN <text>`` ``FINAL``           ``NNNN``               Last line of the response (text may be empty)
  ==============  ===================  ====================  ==================================

* The canonical "command completed successfully" terminator is the 6-byte
  sequence ``b"0000 \\n"``.
* Error terminators are codes in the ``8xxx`` / ``9xxx`` range, also written
  with a SPACE separator.

The parser is defensive: a line that does not match any of the four
prefixes is returned with ``kind=UNKNOWN`` and a ``BirdProtocolError`` is
raised at the :class:`CommandResult` assembly step so callers still get
the raw bytes for inspection.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal

from .exceptions import BirdProtocolError

__all__ = [
    "ReplyLineKind",
    "ReplyLine",
    "ReplySection",
    "CommandResult",
    "parse_line",
    "MAX_COMMAND_LENGTH",
    "MAX_REPLY_LINE_LENGTH",
]

# Hard limits enforced by the daemon (see scripts/bird-protocol-research.md §2.1, §2.2).
MAX_COMMAND_LENGTH: int = 4094  # CLI_RX_BUF_SIZE - 2, excluding the trailing \n.
MAX_REPLY_LINE_LENGTH: int = 512  # CLI_LINE_SIZE — total reply line size including the 5-byte prefix.


class ReplyLineKind(enum.StrEnum):
    """Discrete category of a single reply line, by its wire prefix."""

    ASYNC = "async"  # '+'
    CONTINUATION = "continuation"  # ' ' (same code as previous)
    SECTION_START = "section_start"  # 'NNNN-'
    FINAL = "final"  # 'NNNN '
    UNKNOWN = "unknown"  # defensive fallback — protocol violation


@dataclass(frozen=True, slots=True)
class ReplyLine:
    """A single decoded reply line, plus its original raw bytes."""

    kind: ReplyLineKind
    code: int | None
    text: str
    raw: bytes

    def __post_init__(self) -> None:
        # INVARIANT: SECTION_START and FINAL lines always carry a code in [0, 9999].
        if self.kind in (ReplyLineKind.SECTION_START, ReplyLineKind.FINAL) and (
            self.code is None or not (0 <= self.code <= 9999)
        ):
            raise BirdProtocolError(
                f"line of kind {self.kind.name} must have code in [0, 9999], "
                f"got {self.code!r}",
                raw_line=self.raw,
            )


@dataclass(frozen=True, slots=True)
class ReplySection:
    """All reply lines that share the same numeric code in a single response.

    The first line of a section has ``kind == SECTION_START``; subsequent lines
    have ``kind == CONTINUATION`` (single-space prefix). Both are folded into
    this section so a command parser can iterate ``section.lines`` and get the
    text of every line in that section.
    """

    code: int
    lines: list[ReplyLine] = field(default_factory=list)

    @property
    def texts(self) -> list[str]:
        """Just the ``text`` portion of each line in this section, in order."""
        return [ln.text for ln in self.lines]

    @property
    def raws(self) -> list[bytes]:
        """Just the ``raw`` bytes of each line in this section, in order."""
        return [ln.raw for ln in self.lines]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Complete decoded response to a single BIRD command.

    Attributes
    ----------
    command:
        The wire string sent to the daemon (without trailing ``\\n``).
    sections:
        Sections in arrival order. The first section's first line carries the
        ``NNNN-`` prefix; same-code continuation lines are folded in. Async
        messages are NOT included here — see ``async_messages``.
    async_messages:
        Texts of all ``+``-prefixed messages received while waiting for the
        final line. These are typically unsolicited daemon notices (e.g.
        "Config timeout expired, starting undo").
    final_code:
        Numeric code from the final ``NNNN <text>`` line. ``0`` for plain
        success, ``8xxx`` / ``9xxx`` for errors. Will be ``0`` even when the
        daemon reports a non-error status like ``0003 Reconfigured``.
    final_text:
        Text of the final line (e.g. ``"Reconfigured"``, ``""`` for plain
        ``0000`` success, ``"Network not found"`` for code ``8001``).
    success:
        ``True`` if ``final_code < 8000``.
    """

    command: str
    sections: list[ReplySection]
    async_messages: list[str]
    final_code: int
    final_text: str

    @property
    def success(self) -> bool:
        return self.final_code < 8000

    @property
    def error_message(self) -> str | None:
        """The daemon's error text if the command failed, else ``None``."""
        return None if self.success else self.final_text

    @property
    def raw_text(self) -> str:
        """Reconstruct the original BIRD text output, exactly as ``birdc`` shows it.

        The 4-digit prefix and separator byte on every line are stripped; the
        same-code continuation prefix (a single space) collapses to the text
        that follows it. Blank separator lines become empty lines. Async
        messages are NOT included here.

        The bare ``0000`` success terminator (whose message text is empty) is
        omitted — ``birdc`` does not echo it, and neither do we. Non-empty
        FINAL lines (e.g. ``0013 Daemon is up and running`` or
        ``8001 Network not found``) ARE included.
        """
        out: list[str] = []
        for section in self.sections:
            for ln in section.lines:
                # Skip empty FINAL lines — they produce no visible output.
                if ln.kind is ReplyLineKind.FINAL and not ln.text:
                    continue
                out.append(ln.text)
        return "\n".join(out)

    @property
    def raw_lines(self) -> list[str]:
        """All non-async reply lines as decoded text (one entry per line)."""
        out: list[str] = []
        for section in self.sections:
            for ln in section.lines:
                out.append(ln.text)
        return out

    def section(self, code: int) -> ReplySection | None:
        """Return the first section with the given code, or ``None``."""
        for s in self.sections:
            if s.code == code:
                return s
        return None

    def sections_with(self, code: int) -> list[ReplySection]:
        """All sections with the given code (there can be more than one)."""
        return [s for s in self.sections if s.code == code]


def parse_line(raw: bytes) -> ReplyLine:
    """Decode one raw reply line (without the trailing ``\\n``) into a :class:`ReplyLine`.

    Defensive: if the line does not match any of the four known prefixes, the
    returned :class:`ReplyLine` will have ``kind=UNKNOWN`` and ``code=None``.
    Callers assembling a :class:`CommandResult` should treat UNKNOWN lines as
    a protocol error.
    """
    if not raw:
        # Empty line on the wire — not produced by the daemon but be defensive.
        return ReplyLine(kind=ReplyLineKind.UNKNOWN, code=None, text="", raw=raw)

    first = raw[0:1]

    # 1) Async: '+text'
    if first == b"+":
        return ReplyLine(
            kind=ReplyLineKind.ASYNC,
            code=None,
            text=raw[1:].decode("utf-8", errors="replace"),
            raw=raw,
        )

    # 2) Same-code continuation: ' text'
    if first == b" ":
        return ReplyLine(
            kind=ReplyLineKind.CONTINUATION,
            code=None,
            text=raw[1:].decode("utf-8", errors="replace"),
            raw=raw,
        )

    # 3) / 4) 4-digit code + separator ('-' for section start, ' ' for final)
    if len(raw) >= 5:
        digits = raw[:4]
        sep = raw[4:5]
        if _is_four_digits(digits) and sep in (b"-", b" "):
            code = int(digits.decode("ascii"))
            text = raw[5:].decode("utf-8", errors="replace")
            kind: Literal[ReplyLineKind.SECTION_START, ReplyLineKind.FINAL] = (
                ReplyLineKind.FINAL if sep == b" " else ReplyLineKind.SECTION_START
            )
            return ReplyLine(kind=kind, code=code, text=text, raw=raw)

    # 5) Defensive fallback. BIRD always emits one of the four known prefixes,
    # so reaching here means the daemon (or the wire) is misbehaving.
    return ReplyLine(
        kind=ReplyLineKind.UNKNOWN,
        code=None,
        text=raw.decode("utf-8", errors="replace"),
        raw=raw,
    )


def _is_four_digits(data: bytes) -> bool:
    if len(data) != 4:
        return False
    return all(0x30 <= b <= 0x39 for b in data)  # '0' .. '9'


def assemble_result(command: str, lines: list[ReplyLine]) -> CommandResult:
    """Group a flat list of :class:`ReplyLine` into a :class:`CommandResult`.

    The function is pure — it does no I/O. Sections are formed by:

    * A SECTION_START line begins a new section with its code.
    * Each CONTINUATION line is appended to the currently-open section.
    * The FINAL line is its own one-line section (so ``raw_text`` faithfully
      reproduces the daemon's status text — typically ``""`` for plain
      success, or e.g. ``"Reconfigured"`` / ``"Network not found"``).

    Unknown lines raise :class:`BirdProtocolError`.
    Async lines are routed to ``async_messages`` and do not affect sections.
    """
    sections: list[ReplySection] = []
    async_messages: list[str] = []
    final_code = 0
    final_text = ""
    final_seen = False

    current: ReplySection | None = None

    for ln in lines:
        if ln.kind is ReplyLineKind.ASYNC:
            async_messages.append(ln.text)
            continue

        if ln.kind is ReplyLineKind.UNKNOWN:
            raise BirdProtocolError(
                f"unrecognised reply line: {ln.raw!r}",
                raw_line=ln.raw,
            )

        if ln.kind is ReplyLineKind.SECTION_START:
            # Start a new section. (code is guaranteed non-None by ReplyLine's invariant.)
            assert ln.code is not None
            current = ReplySection(code=ln.code, lines=[ln])
            sections.append(current)
            continue

        if ln.kind is ReplyLineKind.CONTINUATION:
            if current is None:
                # Same-code continuation with no preceding SECTION_START.
                # This happens for the hello banner which is a single FINAL
                # line — but a stray CONTINUATION before any SECTION_START is
                # a protocol violation. Be defensive.
                raise BirdProtocolError(
                    "continuation line arrived before any section start",
                    raw_line=ln.raw,
                )
            current.lines.append(ln)
            continue

        if ln.kind is ReplyLineKind.FINAL:
            assert ln.code is not None
            final_code = ln.code
            final_text = ln.text
            # Fold the final line into its own one-line section so that
            # raw_text faithfully reproduces the daemon's status text
            # (e.g. "Reconfigured" on success or "Network not found" on error).
            sections.append(ReplySection(code=ln.code, lines=[ln]))
            final_seen = True
            continue

    if not final_seen:
        raise BirdProtocolError(
            "command response did not end with a FINAL line — "
            "connection may have been closed mid-response",
        )

    return CommandResult(
        command=command,
        sections=sections,
        async_messages=async_messages,
        final_code=final_code,
        final_text=final_text,
    )
