"""Parser for the ``show symbols`` family.

Wire example (BIRD 3.1.7)::

    1010-static_test2\tprotocol
     worker  \tunknown type
     master6 \trouting table
     master4 \trouting table
     kernel1  \tprotocol
    0000

Field layout (``nest/cmds.c:64``): ``%-8s\\t%s``. So the name is padded
to 8 chars and a literal TAB separates it from the class.

Symbol classes seen in the wild: ``protocol``, ``routing table``,
``filter``, ``function``, ``custom attribute``, ``undefined``,
``unknown type``.
"""

from __future__ import annotations

from ..models import Symbol, SymbolsResult
from ..protocol import CommandResult

__all__ = ["parse_symbols"]


def parse_symbols(result: CommandResult) -> SymbolsResult:
    """Decode a ``show symbols`` response into :class:`SymbolsResult`."""
    symbols: list[Symbol] = []
    section_1010 = result.section(1010)
    if section_1010 is None:
        return SymbolsResult(
            raw_text=result.raw_text,
            command=result.command,
            symbols=[],
        )
    for ln in section_1010.lines:
        text = ln.text
        if "\t" not in text:
            continue
        name, _, sym_class = text.partition("\t")
        symbols.append(Symbol(name=name.strip(), symbol_class=sym_class.strip()))
    return SymbolsResult(
        raw_text=result.raw_text,
        command=result.command,
        symbols=symbols,
    )
