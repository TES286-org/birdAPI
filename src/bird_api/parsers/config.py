"""Parser for the ``configure ...`` family of commands.

Wire examples (BIRD 3.1.7)::

    # configure check "<file>" — success
    0002-Reading configuration from /tmp/foo.conf
    0020 Configuration OK

    # configure "<file>" — success
    0002-Reading configuration from /tmp/foo.conf
    0003 Reconfigured

    # configure "<file>" timeout <sec>
    0002-Reading configuration from /tmp/foo.conf
    0022-Undo scheduled in 1 s
    0003 Reconfigured

    # configure status
    0003-Daemon is up and running
    0000

    # configure confirm / undo
    0018 Reconfiguration confirmed
    0019 Reconfiguration undone

Runtime/parse errors (codes 8xxx/9xxx) are raised by the connection layer
as :class:`BirdCommandError` before this parser is reached.
"""

from __future__ import annotations

import re

from ..models import ConfigResult
from ..protocol import CommandResult

__all__ = ["parse_configure"]

# Match "Reading configuration from <file>"
_READING_RE = re.compile(r"^Reading configuration from\s+(?P<file>\S+)\s*$")
# Match "Undo scheduled in <n> s"
_UNDO_RE = re.compile(r"^Undo scheduled in\s+(\d+)\s+s\s*$")


def parse_configure(result: CommandResult) -> ConfigResult:
    """Decode a ``configure ...`` response into :class:`ConfigResult`."""
    file: str | None = None
    undo_in: int | None = None

    # Find the 0002 "Reading configuration from <file>" line.
    section_0002 = result.section(2)
    if section_0002 is not None:
        for ln in section_0002.lines:
            m = _READING_RE.match(ln.text.strip())
            if m:
                file = m.group("file")
                break

    # Look for the 0022 "Undo scheduled in <n> s" line in any section.
    section_0022 = result.section(22)
    if section_0022 is not None:
        for ln in section_0022.lines:
            m = _UNDO_RE.match(ln.text.strip())
            if m:
                undo_in = int(m.group(1))
                break

    # The "message" depends on the variant of `configure`:
    # * `configure "<file>"`       -> final line is `0003 Reconfigured`
    # * `configure check "<file>"`  -> final line is `0020 Configuration OK`
    # * `configure status`          -> final line is `0000` (empty) and the
    #   actual status text is in a separate `0003-Daemon is up and running`
    #   continuation section. We fall back to that section's text when the
    #   final line is empty.
    message = result.final_text.strip()
    if not message:
        # `configure status` or similar: pull text from the 0003 section.
        section_0003 = result.section(3)
        if section_0003 is not None and section_0003.lines:
            message = section_0003.lines[0].text.strip()

    return ConfigResult(
        raw_text=result.raw_text,
        command=result.command,
        file=file,
        ok=True,
        message=message,
        undo_scheduled_in=undo_in,
    )
