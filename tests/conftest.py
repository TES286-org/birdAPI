"""Shared test utilities.

* :func:`load_fixture` reads a captured raw-bytes fixture from
  ``tests/fixtures/raw/`` and parses it into a :class:`CommandResult`.
* :func:`make_result` builds a :class:`CommandResult` from inline text,
  so unit tests for parsers can live next to the parser without needing
  a fixture file.
* :func:`run_connection_with_bytes` runs a fake connection that yields a
  fixed sequence of reply bytes, so we can unit-test the connection /
  protocol layer without talking to a real BIRD daemon.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from bird_api.exceptions import BirdCommandError, BirdConnectionError, BirdValidationError
from bird_api.protocol import CommandResult, assemble_result, parse_line
from bird_api.validation import is_read_only_command, validate_command_line

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "raw"


def load_fixture(name: str) -> bytes:
    """Load a captured fixture as raw bytes (including ``\\n`` line endings).

    ``name`` should be the fixture's stem without extension, e.g.
    ``"show_status"`` for ``tests/fixtures/raw/show_status.txt``.
    """
    path = FIXTURE_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"fixture not found: {path}")
    return path.read_bytes()


def make_result_from_bytes(raw: bytes, *, command: str = "test") -> CommandResult:
    """Decode a raw wire response (bytes, including ``\\n``) into a CommandResult.

    The bytes should NOT include the initial hello banner — only the
    actual command response. Lines are split on ``\\n`` and parsed via
    :func:`bird_api.protocol.parse_line`; the resulting
    :class:`ReplyLine` list is fed to
    :func:`bird_api.protocol.assemble_result`.
    """
    lines: list = []
    for raw_line in raw.split(b"\n"):
        if not raw_line and raw_line is raw.split(b"\n")[-1]:
            # Drop trailing empty line (final \n produces an empty trailing chunk).
            continue
        lines.append(parse_line(raw_line))
    # Drop a trailing empty entry created by a final \n
    if lines and lines[-1].raw == b"" and lines[-1].kind.name == "UNKNOWN":
        lines.pop()
    return assemble_result(command, lines)


def make_result_from_text(text: str, *, command: str = "test") -> CommandResult:
    """Decode a str response (LF-separated) into a CommandResult."""
    return make_result_from_bytes(text.encode("utf-8"), command=command)


def load_fixture_result(name: str, *, command: str | None = None) -> CommandResult:
    """Load and decode a fixture in one step."""
    return make_result_from_bytes(load_fixture(name), command=command or name)


class FakeBirdConnection:
    """Drop-in replacement for :class:`BirdConnection` that returns canned bytes.

    Tests construct it with a list of raw byte responses (one per
    ``execute()`` call). Calling :meth:`execute` consumes one entry and
    parses it. If the canned response includes a banner line (code
    ``0001``), it is consumed automatically on the first call to
    :meth:`connect`.

    It mirrors the real connection's safety semantics: command-length
    limit, single-line/control-character validation, the read-only
    allowlist, lazy-connect on first call, and raising ``BirdCommandError``
    when the canned response's final code is >= 8000.
    """

    def __init__(self, *, responses: Iterable[bytes], banner: bytes | None = None) -> None:
        self._responses = list(responses)
        self._banner = banner or b"0001 BIRD 3.1.7 ready.\n"
        self._calls: list[str] = []
        self.is_connected = False
        self.server_version: str | None = None
        self.socket_path = "<fake>"
        self.timeout = 30.0
        self.read_only = False

    def connect(self) -> None:
        # Drain the banner.
        banner_lines = self._banner.split(b"\n")
        for raw_line in banner_lines:
            if not raw_line:
                continue
            line = parse_line(raw_line)
            if (
                line.kind.name == "FINAL"
                and line.code == 1
                and line.text.startswith("BIRD ")
                and line.text.endswith(" ready.")
            ):
                self.server_version = line.text[len("BIRD ") : -len(" ready.")]
        self.is_connected = True

    def close(self) -> None:
        self.is_connected = False

    def __enter__(self) -> FakeBirdConnection:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def execute(self, command: str, *, timeout: float | None = None) -> CommandResult:
        # Mirror the real BirdConnection.execute semantics: enforce the
        # 4094-byte command limit, lazy-connect on first call, and raise
        # BirdCommandError if the canned response's final code is >= 8000.
        if len(command) > 4094:
            raise BirdCommandError(
                9000,
                f"Command too long ({len(command)} bytes, max 4094)",
                command=command,
            )
        validate_command_line(command)
        if self.read_only and not is_read_only_command(command):
            raise BirdValidationError(
                f"read-only connection rejects command {command!r}",
                field="command",
                value=command,
            )
        if not self.is_connected:
            self.connect()
        if not self._responses:
            raise BirdConnectionError("no more canned responses available")
        self._calls.append(command)
        raw = self._responses.pop(0)
        # Strip a leading banner if present in the canned response.
        if raw.startswith(b"0001 "):
            # Skip the first line, then continue.
            nl = raw.find(b"\n")
            if nl >= 0:
                raw = raw[nl + 1 :]
        result = make_result_from_bytes(raw, command=command)
        if not result.success:
            raise BirdCommandError(
                result.final_code,
                result.final_text,
                command=command,
            )
        return result

    @property
    def calls(self) -> list[str]:
        """List of commands passed to :meth:`execute`, in order."""
        return list(self._calls)


def make_client_with_responses(
    responses: Iterable[bytes],
    *,
    banner: bytes | None = None,
):
    """Return a :class:`BirdClient` wired to a :class:`FakeBirdConnection`."""
    from bird_api.client import BirdClient

    client = BirdClient.__new__(BirdClient)
    client._conn = FakeBirdConnection(responses=responses, banner=banner)
    return client


__all__ = [
    "FIXTURE_DIR",
    "FakeBirdConnection",
    "load_fixture",
    "load_fixture_result",
    "make_result_from_bytes",
    "make_result_from_text",
    "make_client_with_responses",
]
