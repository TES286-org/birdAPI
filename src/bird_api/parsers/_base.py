"""Helpers shared across all command parsers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from ..exceptions import BirdParseError
from ..protocol import CommandResult, ReplySection

__all__ = [
    "require_section",
    "must_section",
    "parse_bird_timestamp",
    "iter_section_texts",
    "ParseContext",
]


def must_section(result: CommandResult, code: int, *, command: str) -> ReplySection:
    """Return the first section with ``code`` or raise :class:`BirdParseError`."""
    section = result.section(code)
    if section is None:
        raise BirdParseError(
            f"expected a section with code {code} in the {command!r} response "
            f"but none was found",
            raw_text=result.raw_text,
        )
    return section


def require_section(result: CommandResult, code: int) -> ReplySection | None:
    """Return the first section with ``code`` or ``None`` (no error)."""
    return result.section(code)


def iter_section_texts(section: ReplySection | None) -> Iterable[str]:
    """Yield the ``text`` of every line in ``section`` (or nothing if ``None``)."""
    if section is None:
        return
    for ln in section.lines:
        yield ln.text


# BIRD timestamp formats (see nest/cli.c `cli_set_timeformat`):
#   "2026-08-24 13:07:54.017"        long
#   "13:07:46.670"                   short (just time of day)
#   "2026-08-24 13:07:54"            long without millis (used in some places)
# The first one is the default. The short form appears in ``show protocols``
# "Since" column.
_LONG_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$")
_SHORT_TS = re.compile(r"^\d{2}:\d{2}:\d{2}(\.\d+)?$")


def parse_bird_timestamp(text: str) -> datetime | None:
    """Parse a BIRD-formatted timestamp, returning ``None`` if unparseable.

    Accepts both the long form (``YYYY-MM-DD HH:MM:SS[.mmm]``) and the
    short form (``HH:MM:SS[.mmm]``). Short-form timestamps are returned
    with the date component set to ``1900-01-01`` because the daemon
    doesn't emit the date for them.
    """
    text = text.strip()
    if not text or text == "---":
        return None
    if _LONG_TS.match(text):
        # Try with millis first, fall back to seconds.
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    if _SHORT_TS.match(text):
        # Strip millis for strptime consistency.
        for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                return dt.replace(year=1900, month=1, day=1)
            except ValueError:
                continue
    return None


class ParseContext:
    """Tiny helper that bundles the result + raw_text for error messages.

    Mostly here so parser functions can raise with consistent error context
    without passing ``result`` around explicitly.
    """

    def __init__(self, *, command: str, raw_text: str) -> None:
        self.command = command
        self.raw_text = raw_text

    def error(self, message: str) -> BirdParseError:
        return BirdParseError(message, raw_text=self.raw_text)
