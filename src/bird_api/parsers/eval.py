"""Parser for the ``eval <expr>`` command.

Wire example (BIRD 3.1.7)::

    0023 3

The single FINAL line (code 0023) carries the expression's value as text.
Runtime errors (code 8008) are raised by the connection layer as
:class:`BirdCommandError` before this parser is reached.
"""

from __future__ import annotations

from ..models import EvalResult
from ..protocol import CommandResult

__all__ = ["parse_eval"]


def parse_eval(result: CommandResult) -> EvalResult:
    """Decode an ``eval <expr>`` response into :class:`EvalResult`."""
    return EvalResult(
        raw_text=result.raw_text,
        command=result.command,
        value=result.final_text.strip(),
    )
