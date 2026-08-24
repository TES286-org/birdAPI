"""Tests for the high-level BirdClient — using the FakeBirdConnection."""

from __future__ import annotations

import pytest
from conftest import make_client_with_responses

from bird_api import BirdClient
from bird_api.exceptions import BirdCommandError, BirdConnectionError


class TestClientLifecycle:
    def test_connect_and_close(self) -> None:
        client = make_client_with_responses([])
        client.connect()
        assert client.is_connected
        assert client.server_version == "3.1.7"
        client.close()
        assert not client.is_connected

    def test_context_manager(self) -> None:
        client = make_client_with_responses([])
        with client:
            assert client.is_connected
        assert not client.is_connected

    def test_lazy_connect_on_first_command(self) -> None:
        # Provide a single canned "show status" response.
        canned = (
            b"1000-BIRD 3.1.7\n"
            b"1011-Router ID is 10.0.0.1\n"
            b" Hostname is foo\n"
            b" Current server time is 2026-08-24 13:07:54.017\n"
            b" Last reboot on 2026-08-24 13:07:46.670\n"
            b" Last reconfiguration on 2026-08-24 13:07:46.670\n"
            b"0013 Daemon is up and running\n"
        )
        client = make_client_with_responses([canned])
        # No explicit connect call — execute should lazy-connect.
        status = client.show_status()
        assert status.bird_version == "3.1.7"
        assert status.router_id == "10.0.0.1"
        assert status.reconfig_state == "up"
        assert client.is_connected


class TestClientCommands:
    def test_show_status(self) -> None:
        canned = (
            b"1000-BIRD 3.1.7\n"
            b"1011-Router ID is 10.0.0.1\n"
            b" Hostname is foo\n"
            b" Current server time is 2026-08-24 13:07:54.017\n"
            b" Last reboot on 2026-08-24 13:07:46.670\n"
            b" Last reconfiguration on 2026-08-24 13:07:46.670\n"
            b"0013 Daemon is up and running\n"
        )
        client = make_client_with_responses([canned])
        s = client.show_status()
        assert s.bird_version == "3.1.7"
        assert s.router_id == "10.0.0.1"
        assert s.hostname == "foo"
        assert s.reconfig_state == "up"
        assert client._conn.calls == ["show status"]

    def test_show_route_with_all_kwarg(self) -> None:
        canned = b"1007-Table master4:\n0000 \n"
        client = make_client_with_responses([canned])
        client.show_route(all=True)
        assert client._conn.calls == ["show route all"]

        client2 = make_client_with_responses([canned])
        client2.show_route()
        assert client2._conn.calls == ["show route"]

    def test_show_route_prefix(self) -> None:
        canned = b"1007-Table master4:\n0000 \n"
        client = make_client_with_responses([canned])
        client.show_route_prefix("192.0.2.0/24", all=True)
        assert client._conn.calls == ["show route 192.0.2.0/24 all"]

    def test_show_route_for(self) -> None:
        canned = b"1007-Table master4:\n0000 \n"
        client = make_client_with_responses([canned])
        client.show_route_for("8.8.8.8")
        assert client._conn.calls == ["show route for 8.8.8.8"]

    def test_show_route_in(self) -> None:
        canned = b"1007-Table master4:\n0000 \n"
        client = make_client_with_responses([canned])
        client.show_route_in("10.0.0.0/8")
        assert client._conn.calls == ["show route in 10.0.0.0/8"]

    def test_show_route_table(self) -> None:
        canned = b"1007-Table master6:\n0000 \n"
        client = make_client_with_responses([canned])
        client.show_route_table("master6")
        assert client._conn.calls == ["show route table master6"]

    def test_show_route_protocol(self) -> None:
        canned = b"1007-Table master4:\n0000 \n"
        client = make_client_with_responses([canned])
        client.show_route_protocol("static_test")
        assert client._conn.calls == ["show route protocol static_test"]

    def test_show_route_stats(self) -> None:
        canned = b"1007-Table master4:\n0014 Total: 1 of 1 routes for 1 networks in 1 tables\n"
        client = make_client_with_responses([canned])
        r = client.show_route_stats()
        assert r.stats["routes"] == "1"
        assert client._conn.calls == ["show route stats"]

    def test_show_symbols_table_only(self) -> None:
        canned = b"1010-master4 \trouting table\n0000 \n"
        client = make_client_with_responses([canned])
        client.show_symbols(table=True)
        assert client._conn.calls == ["show symbols table"]

    def test_eval(self) -> None:
        canned = b"0023 42\n"
        client = make_client_with_responses([canned])
        ev = client.eval("6*7")
        assert ev.value == "42"
        assert client._conn.calls == ["eval 6*7"]

    def test_configure_check(self) -> None:
        canned = (
            b'0002-Reading configuration from /tmp/foo.conf\n'
            b"0020 Configuration OK\n"
        )
        client = make_client_with_responses([canned])
        c = client.configure("/tmp/foo.conf", check=True)
        assert c.ok is True
        assert c.message == "Configuration OK"
        assert client._conn.calls == ['configure check "/tmp/foo.conf"']

    def test_configure_apply(self) -> None:
        canned = (
            b'0002-Reading configuration from /tmp/foo.conf\n'
            b"0003 Reconfigured\n"
        )
        client = make_client_with_responses([canned])
        c = client.configure("/tmp/foo.conf")
        assert c.message == "Reconfigured"
        assert client._conn.calls == ['configure "/tmp/foo.conf"']

    def test_configure_with_timeout(self) -> None:
        canned = (
            b'0002-Reading configuration from /tmp/foo.conf\n'
            b"0022-Undo scheduled in 5 s\n"
            b"0003 Reconfigured\n"
        )
        client = make_client_with_responses([canned])
        c = client.configure("/tmp/foo.conf", timeout=5)
        assert c.undo_scheduled_in == 5
        assert client._conn.calls == ['configure "/tmp/foo.conf" timeout 5']

    def test_configure_soft(self) -> None:
        canned = b"0003 Reconfigured\n"
        client = make_client_with_responses([canned])
        client.configure("/tmp/foo.conf", soft=True)
        assert client._conn.calls == ['configure soft "/tmp/foo.conf"']

    def test_enable(self) -> None:
        canned = b"0009-direct1: enabled\n0000 \n"
        client = make_client_with_responses([canned])
        a = client.enable("direct1")
        assert "direct1: enabled" in a.per_target
        assert client._conn.calls == ["enable direct1"]

    def test_disable_default_pattern(self) -> None:
        canned = b"0009-direct1: disabled\n0000 \n"
        client = make_client_with_responses([canned])
        client.disable()
        assert client._conn.calls == ["disable all"]

    def test_restart(self) -> None:
        canned = b"0012-direct1: restarted\n0000 \n"
        client = make_client_with_responses([canned])
        client.restart("direct1")
        assert client._conn.calls == ["restart direct1"]

    def test_reload_in(self) -> None:
        canned = b"0015-direct1: reloading\n0000 \n"
        client = make_client_with_responses([canned])
        client.reload("direct1", direction="in")
        assert client._conn.calls == ["reload in direct1"]

    def test_reload_invalid_direction(self) -> None:
        client = make_client_with_responses([])
        with pytest.raises(ValueError, match="direction must be"):
            client.reload("direct1", direction="sideways")  # type: ignore[arg-type]

    def test_dump_invalid_kind(self) -> None:
        client = make_client_with_responses([])
        with pytest.raises(ValueError, match="invalid dump kind"):
            client.dump("unknown_kind", "/tmp/x")

    def test_dump_quotes_file_path(self) -> None:
        canned = b"0013-Dumping resources to /tmp/dump.txt\n0013 Dumped 1 bytes in 0.000 s\n"
        client = make_client_with_responses([canned])
        client.dump("resources", "/tmp/dump.txt")
        assert client._conn.calls == ['dump resources "/tmp/dump.txt"']

    def test_restrict(self) -> None:
        canned = b"0016 Access restricted\n"
        client = make_client_with_responses([canned])
        a = client.restrict()
        assert a.message == "Access restricted"

    def test_down(self) -> None:
        canned = b"0007 Shutdown requested\n"
        client = make_client_with_responses([canned])
        a = client.down()
        assert a.message == "Shutdown requested"

    def test_graceful_restart(self) -> None:
        canned = b"0025 Graceful restart requested\n"
        client = make_client_with_responses([canned])
        a = client.graceful_restart()
        assert a.message == "Graceful restart requested"

    def test_raw_command(self) -> None:
        canned = b"0000 \n"
        client = make_client_with_responses([canned])
        r = client.raw("show route export bgp1")
        assert r.success
        assert client._conn.calls == ["show route export bgp1"]

    def test_raw_command_error_raises(self) -> None:
        canned = b"8001 Network not found\n"
        client = make_client_with_responses([canned])
        with pytest.raises(BirdCommandError) as exc_info:
            client.raw("show route 1.2.3.4/32")
        assert exc_info.value.code == 8001
        assert "Network not found" in str(exc_info.value)
        assert exc_info.value.command == "show route 1.2.3.4/32"

    def test_too_long_command_raises(self) -> None:
        client = make_client_with_responses([])
        # 4095 chars + the "garbage " prefix = > 4094
        long_cmd = "garbage " + "x" * 4095
        with pytest.raises(BirdCommandError, match="Command too long"):
            client.raw(long_cmd)

    def test_no_more_canned_responses_raises(self) -> None:
        client = make_client_with_responses([])
        client.connect()
        with pytest.raises(BirdConnectionError):
            client.show_status()


class TestClientAsyncCallback:
    def test_callback_wired_through_constructor(self) -> None:
        # Verify that the on_async callback is forwarded from BirdClient
        # to its underlying BirdConnection. We can't easily exercise the
        # real callback without a live BIRD daemon (async messages are
        # only produced on certain commands like `configure ... timeout`);
        # the end-to-end test in tests/e2e/ exercises the live path.
        captured: list[str] = []

        client = BirdClient(on_async=captured.append)
        # The BirdConnection stores the callback on a private attribute;
        # we check it's the same callable (using ==, since bound methods
        # do not compare with `is`).
        assert client._conn._on_async == captured.append
