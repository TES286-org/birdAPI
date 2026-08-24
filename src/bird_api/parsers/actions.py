"""Parser for ``enable`` / ``disable`` / ``restart`` / ``reload`` / ``dump`` commands.

Wire examples (BIRD 3.1.7)::

    # disable direct1
    0009-direct1: disabled
    0000

    # enable all
    0010-device1: already enabled
    0009-direct1: enabled
    0000

    # dump resources "/tmp/dump.txt"
    0013-Dumping resources to /tmp/dump.txt
    ... dumped 46997 bytes in 0.000 s
    0013 Dumped 46997 bytes in 0.000 s

    # disable nothing_matching
    8003 No protocols match

The per-target continuation lines all carry their own numeric code (0008
through 0015); they are folded into ``per_target`` verbatim, preserving
the daemon's text exactly. The final line (code 0 on success) is captured
in ``message``.
"""

from __future__ import annotations

from ..models import ActionResult
from ..protocol import CommandResult

__all__ = ["parse_action"]


def parse_action(result: CommandResult) -> ActionResult:
    """Decode an ``enable`` / ``disable`` / ``restart`` / ``reload`` / ``dump`` response.

    All continuation lines (i.e. every non-final, non-async line in the
    response) are concatenated into ``per_target`` verbatim. The final
    line's text becomes ``message``.
    """
    per_target: list[str] = []
    for sec in result.sections:
        for ln in sec.lines:
            # Skip the FINAL line — its text is captured as `message` below
            # to avoid duplicating it in `per_target`.
            if ln.kind.name == "FINAL":
                continue
            text = ln.text
            # Strip a leading tab from dump-progress lines.
            stripped = text.lstrip("\t ")
            if stripped:
                per_target.append(stripped)

    return ActionResult(
        raw_text=result.raw_text,
        command=result.command,
        per_target=per_target,
        message=result.final_text.strip(),
    )
