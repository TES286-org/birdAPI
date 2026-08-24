"""Tests for the wire-protocol parser (bird_api.protocol)."""

from __future__ import annotations

import pytest

from bird_api.exceptions import BirdProtocolError
from bird_api.protocol import (
    ReplyLineKind,
    assemble_result,
    parse_line,
)


class TestParseLine:
    """Unit tests for parse_line — the byte-level reply-line decoder."""

    def test_async_line(self) -> None:
        line = parse_line(b"+Config timeout expired, starting undo")
        assert line.kind is ReplyLineKind.ASYNC
        assert line.code is None
        assert line.text == "Config timeout expired, starting undo"
        assert line.raw == b"+Config timeout expired, starting undo"

    def test_async_empty(self) -> None:
        line = parse_line(b"+")
        assert line.kind is ReplyLineKind.ASYNC
        assert line.text == ""

    def test_continuation_same_code(self) -> None:
        # Leading single space = same-code continuation; the prefix-stripped
        # text is what's left after the space.
        line = parse_line(b" 0.0.0.0/0  unicast [kernel1 13:22:42.389] * (10)")
        assert line.kind is ReplyLineKind.CONTINUATION
        assert line.code is None
        assert line.text == "0.0.0.0/0  unicast [kernel1 13:22:42.389] * (10)"

    def test_continuation_empty_text(self) -> None:
        # Same-code continuation with empty text (cli_separator() output).
        line = parse_line(b" ")
        assert line.kind is ReplyLineKind.CONTINUATION
        assert line.text == ""

    def test_section_start(self) -> None:
        line = parse_line(b"1002-static_test Static     master4    up     13:22:41.388")
        assert line.kind is ReplyLineKind.SECTION_START
        assert line.code == 1002
        assert line.text == "static_test Static     master4    up     13:22:41.388"

    def test_section_start_empty_text(self) -> None:
        # Used as a section separator between different codes.
        line = parse_line(b"1006-")
        assert line.kind is ReplyLineKind.SECTION_START
        assert line.code == 1006
        assert line.text == ""

    def test_final_with_text(self) -> None:
        line = parse_line(b"0013 Daemon is up and running")
        assert line.kind is ReplyLineKind.FINAL
        assert line.code == 13
        assert line.text == "Daemon is up and running"

    def test_final_empty(self) -> None:
        # The canonical success terminator.
        line = parse_line(b"0000 ")
        assert line.kind is ReplyLineKind.FINAL
        assert line.code == 0
        assert line.text == ""

    def test_final_error(self) -> None:
        line = parse_line(b"8001 Network not found")
        assert line.kind is ReplyLineKind.FINAL
        assert line.code == 8001
        assert line.text == "Network not found"

    def test_unknown_garbage(self) -> None:
        # Bytes that don't match any known prefix are marked UNKNOWN.
        line = parse_line(b"!!garbage!!")
        assert line.kind is ReplyLineKind.UNKNOWN
        assert line.code is None

    def test_empty_line(self) -> None:
        # Empty bytes — not produced by the daemon but must not crash.
        line = parse_line(b"")
        assert line.kind is ReplyLineKind.UNKNOWN


class TestAssembleResult:
    """Tests for assemble_result — turning a list of ReplyLines into a CommandResult."""

    def test_simple_success(self) -> None:
        lines = [
            parse_line(b"0001 BIRD 3.1.7 ready."),
        ]
        result = assemble_result("test", lines)
        assert result.command == "test"
        assert result.final_code == 1
        assert result.final_text == "BIRD 3.1.7 ready."
        assert result.success

    def test_multi_section_response(self) -> None:
        # Mimic the show status response.
        lines = [
            parse_line(b"1000-BIRD 3.1.7"),
            parse_line(b"1011-Router ID is 10.0.0.1"),
            parse_line(b" Hostname is foo"),
            parse_line(b"0013 Daemon is up and running"),
        ]
        result = assemble_result("show status", lines)
        assert result.final_code == 13
        assert result.final_text == "Daemon is up and running"
        # Three sections: 1000, 1011, 13 (the final is folded into its own section).
        assert len(result.sections) == 3
        assert result.sections[0].code == 1000
        assert result.sections[1].code == 1011
        assert result.sections[2].code == 13

    def test_raw_text_strips_prefixes(self) -> None:
        lines = [
            parse_line(b"1000-BIRD 3.1.7"),
            parse_line(b"1011-Router ID is 10.0.0.1"),
            parse_line(b" Hostname is foo"),
            parse_line(b"0013 Daemon is up and running"),
        ]
        result = assemble_result("test", lines)
        assert result.raw_text == (
            "BIRD 3.1.7\n"
            "Router ID is 10.0.0.1\n"
            "Hostname is foo\n"
            "Daemon is up and running"
        )

    def test_async_messages_collected_separately(self) -> None:
        lines = [
            parse_line(b"1000-Hello"),
            parse_line(b"+Config timeout expired"),
            parse_line(b" continued line"),
            parse_line(b"0000 "),
        ]
        result = assemble_result("test", lines)
        assert result.async_messages == ["Config timeout expired"]
        # Async lines do NOT appear in raw_text.
        assert "Config timeout expired" not in result.raw_text
        assert result.raw_text == "Hello\ncontinued line"

    def test_no_final_line_raises(self) -> None:
        lines = [parse_line(b"1000-Hello")]
        with pytest.raises(BirdProtocolError):
            assemble_result("test", lines)

    def test_unknown_line_raises(self) -> None:
        lines = [
            parse_line(b"1000-Hello"),
            parse_line(b"!!garbage!!"),
            parse_line(b"0000 "),
        ]
        with pytest.raises(BirdProtocolError):
            assemble_result("test", lines)

    def test_continuation_before_section_start_raises(self) -> None:
        # Same-code continuation with no preceding section start — protocol error.
        lines = [
            parse_line(b" orphan line"),
            parse_line(b"0000 "),
        ]
        with pytest.raises(BirdProtocolError):
            assemble_result("test", lines)


class TestCommandResult:
    """Tests for the convenience accessors on CommandResult."""

    def test_section_lookup(self) -> None:
        lines = [
            parse_line(b"1000-Hello"),
            parse_line(b"1011-World"),
            parse_line(b"0000 "),
        ]
        result = assemble_result("test", lines)
        assert result.section(1000) is not None
        assert result.section(1000).texts == ["Hello"]
        assert result.section(999) is None

    def test_sections_with_multiple(self) -> None:
        # Two 1007 sections (routes).
        lines = [
            parse_line(b"1007-Route1"),
            parse_line(b"1012-attr1"),
            parse_line(b"1007-Route2"),
            parse_line(b"0000 "),
        ]
        result = assemble_result("test", lines)
        sections_1007 = result.sections_with(1007)
        assert len(sections_1007) == 2
        assert sections_1007[0].texts == ["Route1"]
        assert sections_1007[1].texts == ["Route2"]

    def test_success_property(self) -> None:
        lines_ok = [parse_line(b"0000 ")]
        result_ok = assemble_result("test", lines_ok)
        assert result_ok.success

        lines_err = [parse_line(b"8001 Network not found")]
        result_err = assemble_result("test", lines_err)
        assert not result_err.success
        assert result_err.error_message == "Network not found"
