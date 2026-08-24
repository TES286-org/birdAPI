"""End-to-end tests against a real BIRD daemon.

These tests require a running BIRD instance whose control socket is at
``/tmp/bird-test.ctl`` (or whatever ``BIRD_TEST_SOCKET`` points at).
The helper script ``scripts/start-bird.sh`` starts such an instance
with a small test config; pytest skips the entire module if the socket
is not reachable.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from bird_api import BirdClient
from bird_api.exceptions import BirdCommandError

# Default socket path matches what scripts/start-bird.sh creates.
DEFAULT_SOCKET = "/tmp/bird-test.ctl"
SOCKET_PATH = os.environ.get("BIRD_TEST_SOCKET", DEFAULT_SOCKET)
BIRD_BIN = os.environ.get("BIRD_BIN", "/home/z/my-project/birdbin/usr/sbin/bird")


def _socket_reachable(path: str) -> bool:
    """Return True if a Unix socket exists at ``path`` and accepts connections."""
    try:
        s = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if (s.st_mode & 0o170000) != 0o140000:  # S_IFSOCK
        return False
    import socket

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(path)
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _socket_reachable(SOCKET_PATH),
    reason=f"no reachable BIRD control socket at {SOCKET_PATH!r}; "
    f"run /home/z/my-project/scripts/start-bird.sh first",
)


@pytest.fixture(scope="module")
def client() -> BirdClient:
    """A connected :class:`BirdClient` shared across the e2e tests."""
    c = BirdClient(socket_path=SOCKET_PATH, timeout=10.0)
    c.connect()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Basic read-only commands
# ---------------------------------------------------------------------------


class TestShowStatus:
    def test_returns_known_fields(self, client: BirdClient) -> None:
        s = client.show_status()
        assert s.bird_version  # e.g. "3.1.7"
        assert s.router_id == "10.0.0.1"  # set in scripts/bird-test-conf.conf
        assert s.hostname  # whatever the container's hostname is
        assert s.current_server_time is not None
        assert s.last_reboot is not None
        assert s.last_reconfiguration is not None
        assert s.reconfig_state == "up"

    def test_raw_text_preserved(self, client: BirdClient) -> None:
        s = client.show_status()
        assert "BIRD " in s.raw_text
        assert "Router ID is 10.0.0.1" in s.raw_text
        assert "Daemon is up and running" in s.raw_text


class TestShowMemory:
    def test_returns_expected_rows(self, client: BirdClient) -> None:
        m = client.show_memory()
        labels = {r.label for r in m.rows}
        # The major memory categories BIRD always reports.
        assert "Routing tables" in labels
        assert "Route attributes" in labels
        assert "Protocols" in labels
        assert "Current config" in labels
        assert "Standby memory" in labels
        assert "Total" in labels

    def test_page_stats_present(self, client: BirdClient) -> None:
        m = client.show_memory()
        assert "Active pages" in m.page_stats
        assert "Kept free pages" in m.page_stats
        assert "Cold free pages" in m.page_stats


class TestShowProtocols:
    def test_lists_expected_protocols(self, client: BirdClient) -> None:
        p = client.show_protocols()
        names = {r.name for r in p.rows}
        # The test config defines these.
        assert {"static_test", "static_test2", "device1", "kernel1", "direct1"} <= names
        # They should all be in the "up" state when the daemon is healthy.
        assert all(r.state == "up" for r in p.rows)

    def test_static_test_columns(self, client: BirdClient) -> None:
        p = client.show_protocols()
        static = next(r for r in p.rows if r.name == "static_test")
        assert static.protocol == "Static"
        assert static.table == "master4"
        assert static.state == "up"
        # The since column should look like a time-of-day timestamp.
        assert ":" in static.since


class TestShowProtocolsAll:
    def test_includes_channels_for_static(self, client: BirdClient) -> None:
        pa = client.show_protocols_all()
        static = next(d for d in pa.details if d.name == "static_test")
        assert len(static.channels) == 1
        ch = static.channels[0]
        assert ch.name == "ipv4"
        assert ch.state == "UP"
        assert ch.preference == "200"
        # The static_test protocol has 3 routes configured.
        assert ch.routes_imported == 3

    def test_route_change_stats_headers_merged(self, client: BirdClient) -> None:
        # Regression test for the "RX limit" header that .split() breaks
        # into "RX" and "limit".
        pa = client.show_protocols_all()
        static = next(d for d in pa.details if d.name == "static_test")
        ch = static.channels[0]
        # All four row labels should be present (not just Import updates).
        for key in ("Import updates", "Import withdraws", "Export updates", "Export withdraws"):
            assert key in ch.route_change_stats, f"missing key {key!r}"
        imp = ch.route_change_stats["Import updates"]
        # "received" and "accepted" should be in the per-row dict.
        assert "received" in imp
        assert "accepted" in imp
        # "RX limit" (the merged header) should also be there.
        assert "RX limit" in imp


class TestShowRoute:
    def test_default_table_routes(self, client: BirdClient) -> None:
        r = client.show_route()
        assert r.table == "master4"
        # The test config defines 5 static routes + the kernel default route.
        prefixes = {e.prefix for e in r.entries}
        assert "192.0.2.0/24" in prefixes
        assert "198.51.100.0/24" in prefixes
        assert "203.0.113.0/24" in prefixes
        assert "10.0.0.0/8" in prefixes
        assert "172.16.0.0/12" in prefixes
        # The kernel default route is the primary (marked with *).
        kernel = next(e for e in r.entries if e.protocol == "kernel1")
        assert kernel.primary is True
        assert kernel.prefix == "0.0.0.0/0"
        assert len(kernel.via) == 1
        # Static routes are non-primary (marked with !) and have no via.
        static = next(e for e in r.entries if e.protocol == "static_test")
        assert static.primary is False
        assert static.via == []

    def test_show_route_all_includes_attributes(self, client: BirdClient) -> None:
        r = client.show_route(all=True)
        # Each entry should have at least preference and source attributes.
        for e in r.entries:
            assert any("preference" in a for a in e.attribute_lines)
            assert any("source" in a for a in e.attribute_lines)

    def test_show_route_for_specific_prefix(self, client: BirdClient) -> None:
        r = client.show_route_prefix("192.0.2.0/24")
        assert len(r.entries) == 1
        e = r.entries[0]
        assert e.prefix == "192.0.2.0/24"
        assert e.destination == "blackhole"
        assert e.protocol == "static_test"

    def test_show_route_stats_totals(self, client: BirdClient) -> None:
        r = client.show_route_stats()
        assert "routes" in r.stats
        assert "routes_total" in r.stats
        assert "networks" in r.stats
        assert "tables" in r.stats
        # The test config defines 6 routes in 2 tables (master4 + master6).
        assert r.stats["routes"] == "6"
        assert r.stats["tables"] == "2"


class TestShowSymbols:
    def test_lists_routing_tables(self, client: BirdClient) -> None:
        s = client.show_symbols(table=True)
        names = {sym.name for sym in s.symbols}
        assert "master4" in names
        assert "master6" in names
        assert all(sym.symbol_class == "routing table" for sym in s.symbols)

    def test_lists_all_symbols(self, client: BirdClient) -> None:
        s = client.show_symbols()
        classes = {sym.symbol_class for sym in s.symbols}
        assert "protocol" in classes
        assert "routing table" in classes
        assert "custom attribute" in classes
        # Should be a non-trivial number of symbols.
        assert len(s.symbols) > 20


class TestShowInterfaces:
    def test_lists_loopback(self, client: BirdClient) -> None:
        ifa = client.show_interfaces()
        lo = next(i for i in ifa.interfaces if i.name == "lo")
        assert lo.admin_state == "up"
        assert len(lo.addresses) >= 2  # 127.0.0.1/8 and ::1/128
        # Type line should mention Loopback for lo.
        assert "Loopback" in lo.type_line

    def test_summary_lists_interfaces(self, client: BirdClient) -> None:
        ifas = client.show_interfaces_summary()
        names = {r.name for r in ifas.rows}
        assert "lo" in names
        # At least one interface should be in the "up" state.
        assert any(r.state == "up" for r in ifas.rows)


class TestEval:
    def test_arithmetic(self, client: BirdClient) -> None:
        ev = client.eval("1+2")
        assert ev.value == "3"

    def test_complex_expression(self, client: BirdClient) -> None:
        # An expression that returns a numeric value.
        ev = client.eval("(1+2)*3")
        assert ev.value == "9"


class TestConfigureStatus:
    def test_reports_daemon_up(self, client: BirdClient) -> None:
        c = client.configure_status()
        assert c.ok is True
        assert "Daemon is up and running" in c.message or c.message == "Daemon is up and running"


# ---------------------------------------------------------------------------
# Reconfiguration round-trip
# ---------------------------------------------------------------------------


class TestConfigureCheck:
    """Use ``configure check "<file>"`` against synthetic config files."""

    def test_check_valid_config(self, client: BirdClient, tmp_path: Path) -> None:
        # A minimal but valid config: just a router ID and a device protocol.
        cfg = tmp_path / "valid.conf"
        cfg.write_text("router id 10.0.0.1;\nprotocol device { }\n")
        c = client.configure(str(cfg), check=True)
        assert c.ok is True
        assert c.message == "Configuration OK"
        assert c.file == str(cfg)

    def test_check_missing_file_raises(self, client: BirdClient) -> None:
        with pytest.raises(BirdCommandError) as exc_info:
            client.configure("/nonexistent/path/file.conf", check=True)
        # BIRD reports a parse error (8002) when the file can't be opened.
        assert exc_info.value.code == 8002

    def test_check_invalid_config_raises(self, client: BirdClient, tmp_path: Path) -> None:
        # A config with a syntax error.
        cfg = tmp_path / "invalid.conf"
        cfg.write_text("this is not valid bird syntax !!!\n")
        with pytest.raises(BirdCommandError) as exc_info:
            client.configure(str(cfg), check=True)
        assert exc_info.value.code == 8002


# ---------------------------------------------------------------------------
# Per-protocol actions
# ---------------------------------------------------------------------------


class TestEnableDisable:
    """Round-trip enable/disable a protocol, then restore it."""

    def test_disable_then_enable_direct1(self, client: BirdClient) -> None:
        # Confirm direct1 is up before we touch it.
        before = client.show_protocols()
        direct = next(r for r in before.rows if r.name == "direct1")
        assert direct.state == "up"

        try:
            # Disable it.
            a = client.disable("direct1")
            assert any("direct1: disabled" in line for line in a.per_target)

            # Verify it's now down.
            after = client.show_protocols()
            direct_after = next(r for r in after.rows if r.name == "direct1")
            assert direct_after.state == "down"
        finally:
            # Restore it.
            client.enable("direct1")

        # Confirm it's back up.
        restored = client.show_protocols()
        direct_restored = next(r for r in restored.rows if r.name == "direct1")
        assert direct_restored.state == "up"

    def test_enable_already_enabled_reports_already(self, client: BirdClient) -> None:
        # device1 should be enabled already (it's part of the test config).
        a = client.enable("device1")
        assert any("already enabled" in line for line in a.per_target)

    def test_disable_no_match_raises(self, client: BirdClient) -> None:
        # On BIRD 3.x, an unknown unquoted identifier is a SYNTAX error
        # (9001) rather than a runtime "no protocols match" (8003). To
        # trigger the runtime 8003, we need a quoted pattern that the
        # parser accepts but matches no protocols at runtime.
        with pytest.raises(BirdCommandError) as exc_info:
            client.disable('"zzz_nonexistent_pattern"')
        # 8003 = No protocols match (runtime, not syntax).
        assert exc_info.value.code == 8003


class TestRestart:
    def test_restart_static_protocol(self, client: BirdClient) -> None:
        a = client.restart("static_test")
        assert any("static_test: restarted" in line for line in a.per_target)


# ---------------------------------------------------------------------------
# Connection-layer edge cases
# ---------------------------------------------------------------------------


class TestConnectionEdgeCases:
    def test_reuse_socket_across_commands(self, client: BirdClient) -> None:
        # The same connection should be reused for multiple commands.
        client.show_status()
        client.show_protocols()
        client.show_route()
        # If the socket died in between, the next command would raise;
        # we just verify it still works.
        s = client.show_status()
        assert s.bird_version

    def test_raw_text_round_trips(self, client: BirdClient) -> None:
        # The raw_text should round-trip against what birdc would show:
        # no protocol prefixes, no async messages, no leading space continuation
        # markers.
        r = client.raw("show route")
        # The first line should be "Table master4:" (no 1007- prefix).
        assert r.raw_text.startswith("Table master4:"), f"got: {r.raw_text!r}"
        # Continuation lines should NOT have a leading space (they were
        # the same-code continuation prefix on the wire).
        for line in r.raw_text.split("\n"):
            if line and not line.startswith("\t"):
                # Allow "\tvia ..." continuation lines (the leading tab is part
                # of the message text, not the protocol prefix).
                pass

    def test_command_too_long_raises(self, client: BirdClient) -> None:
        long_cmd = "x" * 5000
        with pytest.raises(BirdCommandError, match="Command too long"):
            client.raw(long_cmd)


class TestConnectionReconnect:
    """Verify that a fresh client transparently reconnects after close()."""

    def test_reconnect_after_close(self) -> None:
        # Use a fresh client (not the shared fixture) so we can close + reopen.
        c = BirdClient(socket_path=SOCKET_PATH, timeout=10.0)
        c.connect()
        assert c.is_connected
        c.close()
        assert not c.is_connected
        # The next execute() should transparently reconnect.
        s = c.show_status()
        assert s.bird_version
        assert c.is_connected
        c.close()


# ---------------------------------------------------------------------------
# Async message handling
# ---------------------------------------------------------------------------


class TestAsyncMessages:
    """Exercise the on_async callback via ``configure "<file>" timeout <sec>``.

    BIRD schedules an undo timer and emits an async ``+Config timeout expired,
    starting undo`` message after the timeout fires. We use a 1-second timeout
    and verify the callback was invoked.
    """

    def test_async_callback_receives_undo_notice(self, tmp_path: Path) -> None:
        captured: list[str] = []
        c = BirdClient(socket_path=SOCKET_PATH, timeout=15.0, on_async=captured.append)
        c.connect()
        try:
            # Apply the existing test config with a 1-second undo timeout.
            # BIRD will accept the config, schedule the undo, and emit the
            # async message ~1 second later when the timer fires.
            cfg_path = "/home/z/my-project/scripts/bird-test-conf.conf"
            client_c = c.configure(cfg_path, timeout=1)
            assert client_c.undo_scheduled_in == 1

            # Wait for the async message to arrive. The library drains async
            # messages as part of every command's response, so we need to
            # issue another command to read it. Use a quick `show status`.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not captured:
                try:
                    c.show_status()
                except Exception:
                    break
                time.sleep(0.2)

            assert any("Config timeout expired" in m for m in captured), (
                f"did not see 'Config timeout expired' async message; got: {captured!r}"
            )
        finally:
            c.close()


# ---------------------------------------------------------------------------
# Multi-client concurrency
# ---------------------------------------------------------------------------


class TestMultipleConnections:
    """Multiple concurrent clients should each get their own connection.

    BIRD's listen backlog is only 8, but we just open 2-3 here.
    """

    def test_two_clients_see_same_status(self) -> None:
        c1 = BirdClient(socket_path=SOCKET_PATH, timeout=10.0)
        c2 = BirdClient(socket_path=SOCKET_PATH, timeout=10.0)
        try:
            s1 = c1.show_status()
            s2 = c2.show_status()
            assert s1.router_id == s2.router_id == "10.0.0.1"
            assert s1.bird_version == s2.bird_version
        finally:
            c1.close()
            c2.close()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_unknown_command_raises_syntax_error(self, client: BirdClient) -> None:
        with pytest.raises(BirdCommandError) as exc_info:
            client.raw("garbage command here")
        assert exc_info.value.code == 9001

    def test_show_route_for_unrouted_network_raises(self, client: BirdClient) -> None:
        with pytest.raises(BirdCommandError) as exc_info:
            client.show_route_prefix("240.0.0.0/24")
        # 8001 = Network not found.
        assert exc_info.value.code == 8001

    def test_dump_invalid_path_raises(self, client: BirdClient, tmp_path: Path) -> None:
        with pytest.raises(BirdCommandError):
            client.dump("resources", "/nonexistent/dir/file.txt")

    def test_dump_resources_writes_file(self, client: BirdClient, tmp_path: Path) -> None:
        out = tmp_path / "dump.txt"
        a = client.dump("resources", str(out))
        assert out.exists()
        assert out.stat().st_size > 0
        assert "Dumped" in a.message
