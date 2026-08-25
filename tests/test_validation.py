"""Tests for the injection-prevention layer (:mod:`bird_api.validation`).

The threat model (verified against a live BIRD 3.1.7 daemon): the control
protocol executes every ``\\n``-terminated line as an independent command,
so any caller-controlled value containing a newline can smuggle a second
command — e.g. ``"10.0.0.0/8\\ndown"`` would shut the daemon down. These
tests throw a battery of classic injection payloads at every validator and
assert each one is rejected *before* any I/O could happen.
"""

from __future__ import annotations

import ipaddress

import pytest

from bird_api.exceptions import BirdValidationError
from bird_api.validation import (
    format_pattern,
    validate_address,
    validate_channel_arg,
    validate_choice,
    validate_command_line,
    validate_filter_expression,
    validate_identifier,
    validate_path,
    validate_pattern,
    validate_positive_int,
    validate_prefix,
)

# --- injection payloads -------------------------------------------------------

NEWLINE_PAYLOADS = [
    "10.0.0.0/8\ndown",
    "bgp1\nshow status",
    "bgp1\r\ndown",
    "static1\nconfigure undo",
    "value\rdisable all",
    "a\x00b",  # NUL
    "a\x1b[2J",  # escape sequence
    "a\x7fb",  # DEL
    "line1\x0bline2",  # vertical tab
    "line1\x0cline2",  # form feed
]

BAD_IDENTIFIER_PAYLOADS = NEWLINE_PAYLOADS + [
    "bgp 1",  # embedded space -> token smuggling
    'bgp"1',  # quote breakout
    "bgp;down",
    "bgp|down",
    "bgp$(down)",
    "bgp`down`",
    "bgp1 extra",
    "-bgp1",
    "1bgp",  # must not start with a digit
    "bgp-1",
    "bgp.1",
    "bgp/1",
    "bgp*",
    "bgp?",
    "héllo",  # non-ASCII
    "",
    "  ",
]

# ---------------------------------------------------------------------------


class TestValidateIdentifier:
    @pytest.mark.parametrize("payload", BAD_IDENTIFIER_PAYLOADS)
    def test_rejects_injection_payloads(self, payload: str) -> None:
        with pytest.raises(BirdValidationError):
            validate_identifier(payload, field="protocol name")

    @pytest.mark.parametrize(
        "good",
        ["bgp1", "static_test", "_private", "A", "master4", "peer_b", "x" * 64],
    )
    def test_accepts_valid_identifiers(self, good: str) -> None:
        assert validate_identifier(good) == good

    def test_rejects_overlong(self) -> None:
        with pytest.raises(BirdValidationError, match="64"):
            validate_identifier("x" * 65)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(BirdValidationError):
            validate_identifier(123)  # type: ignore[arg-type]


class TestValidateCommandLine:
    @pytest.mark.parametrize(
        "payload",
        NEWLINE_PAYLOADS + ["", " " * 5000, "x" * 5000],
    )
    def test_rejects_unsafe_commands(self, payload: str) -> None:
        with pytest.raises(BirdValidationError):
            validate_command_line(payload)

    def test_accepts_normal_command(self) -> None:
        assert validate_command_line("show protocols all") == "show protocols all"

    def test_semicolons_allowed_single_line(self) -> None:
        # Semicolons cannot smuggle a second command (framing is \n only);
        # worst case is a daemon-side syntax error for the single line.
        assert validate_command_line("show status; down") == "show status; down"

    def test_error_carries_field_and_value(self) -> None:
        with pytest.raises(BirdValidationError) as excinfo:
            validate_command_line("down\n")
        assert excinfo.value.field == "command"
        assert excinfo.value.value == "down\n"


class TestValidatePrefix:
    @pytest.mark.parametrize(
        "payload",
        NEWLINE_PAYLOADS
        + [
            "192.0.2.0/24 extra",
            "192.0.2.0/33",
            "not-an-ip",
            "192.0.2.1/24",  # host bits set
            "10.0.0.0/8/",
            "::1/129",
            "192.0.2.0/24; down",
        ],
    )
    def test_rejects_injection_and_garbage(self, payload: str) -> None:
        with pytest.raises(BirdValidationError):
            validate_prefix(payload)

    def test_rejects_host_bits_like_bird(self) -> None:
        # BIRD itself: "Invalid IPv4 prefix 192.0.2.1/24, maybe you wanted 192.0.2.0/24"
        with pytest.raises(BirdValidationError, match="host bits"):
            validate_prefix("192.0.2.1/24")

    @pytest.mark.parametrize(
        ("raw", "canonical"),
        [
            ("192.0.2.0/24", "192.0.2.0/24"),
            ("2001:db8::/32", "2001:db8::/32"),
            ("192.0.2.1", "192.0.2.1/32"),  # bare IP -> host route
            ("::1", "::1/128"),
            (" 10.0.0.0/8 ", "10.0.0.0/8"),  # surrounding whitespace tolerated
        ],
    )
    def test_normalises(self, raw: str, canonical: str) -> None:
        assert validate_prefix(raw) == canonical

    def test_accepts_ipaddress_objects(self) -> None:
        assert validate_prefix(ipaddress.ip_network("192.0.2.0/24")) == "192.0.2.0/24"
        assert validate_prefix(ipaddress.ip_address("192.0.2.7")) == "192.0.2.7/32"


class TestValidateAddress:
    @pytest.mark.parametrize(
        "payload",
        NEWLINE_PAYLOADS + ["192.0.2.0/24", "not-an-ip", "192.0.2.1 down", ""],
    )
    def test_rejects(self, payload: str) -> None:
        with pytest.raises(BirdValidationError):
            validate_address(payload)

    def test_accepts(self) -> None:
        assert validate_address("192.0.2.1") == "192.0.2.1"
        assert validate_address("2001:db8::1") == "2001:db8::1"
        assert validate_address(ipaddress.ip_address("10.0.0.1")) == "10.0.0.1"


class TestValidatePattern:
    @pytest.mark.parametrize(
        "payload",
        NEWLINE_PAYLOADS + ['bgp"*', 'x" y', "bgp*", "bgp?", "bgp*", ""],
    )
    def test_wildcards_ok_but_not_quotes(self, payload: str) -> None:
        if '"' in payload or any(ord(c) < 0x20 or c == "\x7f" for c in payload) or not payload:
            with pytest.raises(BirdValidationError):
                validate_pattern(payload)
        else:
            assert validate_pattern(payload) == payload

    def test_rejects_non_ascii(self) -> None:
        with pytest.raises(BirdValidationError):
            validate_pattern("bgp€*")


class TestFormatPattern:
    def test_bare_identifier_unquoted(self) -> None:
        assert format_pattern("static_test") == "static_test"

    def test_all_keyword_unquoted(self) -> None:
        assert format_pattern("all") == "all"

    def test_wildcard_quoted(self) -> None:
        assert format_pattern("bgp*") == '"bgp*"'

    def test_injection_in_pattern_rejected(self) -> None:
        with pytest.raises(BirdValidationError):
            format_pattern('bgp*" | down')

    def test_question_mark_quoted(self) -> None:
        assert format_pattern("ospf?") == '"ospf?"'


class TestValidateFilterExpression:
    @pytest.mark.parametrize("payload", NEWLINE_PAYLOADS + ["", "  ", "héllo"])
    def test_rejects(self, payload: str) -> None:
        with pytest.raises(BirdValidationError):
            validate_filter_expression(payload)

    def test_accepts_realistic_expressions(self) -> None:
        assert validate_filter_expression('proto = "bgp1"') == 'proto = "bgp1"'
        assert validate_filter_expression("net ~ [10.0.0.0/8+]") == "net ~ [10.0.0.0/8+]"
        assert validate_filter_expression("(net.len >= 16 && net.len <= 32)") == (
            "(net.len >= 16 && net.len <= 32)"
        )

    def test_length_cap(self) -> None:
        with pytest.raises(BirdValidationError, match="2048"):
            validate_filter_expression("x" * 2049)


class TestValidatePath:
    @pytest.mark.parametrize(
        "payload",
        NEWLINE_PAYLOADS + ['/etc/passwd" down', '/tmp/x";down', ""],
    )
    def test_rejects(self, payload: str) -> None:
        with pytest.raises(BirdValidationError):
            validate_path(payload)

    def test_accepts_spaces(self) -> None:
        assert validate_path("/tmp/my conf.conf") == "/tmp/my conf.conf"


class TestValidateChannelArg:
    @pytest.mark.parametrize("payload", NEWLINE_PAYLOADS + ["a.b.c", ".a", "a.", "a b", ""])
    def test_rejects(self, payload: str) -> None:
        with pytest.raises(BirdValidationError):
            validate_channel_arg(payload)

    @pytest.mark.parametrize("good", ["static_test", "static_test.ipv4", "peer_b.ipv4"])
    def test_accepts(self, good: str) -> None:
        assert validate_channel_arg(good) == good


class TestValidateChoiceAndInt:
    def test_choice(self) -> None:
        assert validate_choice("in", ("both", "in", "out")) == "in"
        with pytest.raises(BirdValidationError):
            validate_choice("sideways", ("both", "in", "out"))

    def test_positive_int(self) -> None:
        assert validate_positive_int(5) == 5
        for bad in (-1, "5", 1.5, True, None):
            with pytest.raises(BirdValidationError):
                validate_positive_int(bad)  # type: ignore[arg-type]


class TestValidationErrorSemantics:
    def test_is_value_error_and_bird_error(self) -> None:
        from bird_api import BirdError

        assert issubclass(BirdValidationError, BirdError)
        assert issubclass(BirdValidationError, ValueError)

    def test_message_mentions_injection_for_control_chars(self) -> None:
        with pytest.raises(BirdValidationError, match="injection"):
            validate_identifier("a\nb")
