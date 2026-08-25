"""Tests for the high-level BirdClient — using the FakeBirdConnection."""

from __future__ import annotations

import pytest
from conftest import FakeBirdConnection, make_client_with_responses

from bird_api import BirdClient, RouteFilter, RouteQuery
from bird_api.exceptions import BirdCommandError, BirdConnectionError, BirdValidationError


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
        with pytest.raises(ValueError, match="direction"):
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


# ---------------------------------------------------------------------------
# Injection-safety integration (validation wired into every client method)
# ---------------------------------------------------------------------------

# A minimal well-formed "show route" response for reuse below.
_ROUTE_OK = (
    b"1007-Table master4:\n"
    b" 192.0.2.0/24         blackhole [static_test 13:22:41.388] ! (200)\n"
    b"0000 \n"
)
_PROTOCOLS_OK = (
    b"2002-Name       Proto      Table      State  Since         Info\n"
    b"1002-static_test Static     master4    up     13:22:41.388  \n"
    b"0000 \n"
)


class TestClientInjectionGuards:
    """Every user-facing input slot must validate BEFORE any I/O happens."""

    def _client(self) -> tuple[BirdClient, FakeBirdConnection]:
        client = make_client_with_responses([_ROUTE_OK])
        return client, client._conn  # type: ignore[return-value]

    def test_show_route_prefix_rejects_newline(self) -> None:
        client, conn = self._client()
        with pytest.raises(BirdValidationError, match="injection"):
            client.show_route_prefix("10.0.0.0/8\ndown")
        assert conn.calls == []  # nothing reached the wire

    def test_show_route_for_rejects_garbage(self) -> None:
        client, conn = self._client()
        with pytest.raises(BirdValidationError):
            client.show_route_for("not-an-ip; down")
        assert conn.calls == []

    def test_show_route_in_rejects_host_bits(self) -> None:
        client, conn = self._client()
        with pytest.raises(BirdValidationError, match="host bits"):
            client.show_route_in("192.0.2.1/24")
        assert conn.calls == []

    def test_show_route_table_rejects_spaces(self) -> None:
        client, conn = self._client()
        with pytest.raises(BirdValidationError):
            client.show_route_table("master4 extra")
        assert conn.calls == []

    def test_show_route_protocol_rejects_wildcard(self) -> None:
        # `show route protocol` takes a symbol, not a pattern.
        client, conn = self._client()
        with pytest.raises(BirdValidationError):
            client.show_route_protocol("bgp*")
        assert conn.calls == []

    def test_eval_rejects_newline(self) -> None:
        client, conn = self._client()
        with pytest.raises(BirdValidationError):
            client.eval("1+2\ndown")
        assert conn.calls == []

    def test_configure_rejects_quote_breakout(self) -> None:
        client, conn = self._client()
        with pytest.raises(BirdValidationError):
            client.configure('/tmp/x.conf" down')
        assert conn.calls == []

    def test_enable_rejects_newline_pattern(self) -> None:
        client, conn = self._client()
        with pytest.raises(BirdValidationError):
            client.enable("bgp*\ndown")
        assert conn.calls == []

    def test_dump_rejects_bad_kind(self) -> None:
        client, conn = self._client()
        with pytest.raises(BirdValidationError):
            client.dump("everything", "/tmp/dump.txt")
        assert conn.calls == []

    def test_raw_rejects_newline(self) -> None:
        client, conn = self._client()
        with pytest.raises(BirdValidationError):
            client.raw("show status\ndown")
        assert conn.calls == []

    def test_bgp_sessions_rejects_bad_pattern(self) -> None:
        client, conn = self._client()
        with pytest.raises(BirdValidationError):
            client.bgp_sessions(pattern="peer*\ndown")
        assert conn.calls == []

    def test_route_lookup_rejects_bad_address(self) -> None:
        client, conn = self._client()
        with pytest.raises(BirdValidationError):
            client.route_lookup("10.0.0.0/8")
        assert conn.calls == []


class TestClientPatternQuoting:
    def test_pattern_quoted_on_wire(self) -> None:
        client = make_client_with_responses([_PROTOCOLS_OK])
        client.show_protocols(pattern="static*")
        assert client._conn.calls == ['show protocols "static*"']  # type: ignore[attr-defined]

    def test_bare_identifier_pattern_unquoted(self) -> None:
        client = make_client_with_responses([_PROTOCOLS_OK])
        client.show_protocols(pattern="static_test")
        assert client._conn.calls == ["show protocols static_test"]  # type: ignore[attr-defined]

    def test_all_keyword_unquoted(self) -> None:
        client = make_client_with_responses([_PROTOCOLS_OK])
        client.disable("all")
        assert client._conn.calls == ["disable all"]  # type: ignore[attr-defined]

    def test_wildcard_quoted_for_actions(self) -> None:
        canned = b"0019-static_test: disabled\n0000 \n"
        client = make_client_with_responses([canned])
        client.disable("static*")
        assert client._conn.calls == ['disable "static*"']  # type: ignore[attr-defined]

    def test_bgp_sessions_pattern(self) -> None:
        client = make_client_with_responses([_PROTOCOLS_OK])
        client.bgp_sessions(pattern="peer_*")
        assert client._conn.calls == ['show protocols all "peer_*"']  # type: ignore[attr-defined]


class TestClientReadOnly:
    def test_show_commands_pass(self) -> None:
        client = make_client_with_responses([_PROTOCOLS_OK, _ROUTE_OK])
        client._conn.read_only = True  # type: ignore[attr-defined]
        client.show_protocols()
        client.show_route_prefix("192.0.2.0/24")
        assert len(client._conn.calls) == 2  # type: ignore[attr-defined]

    def test_mutating_commands_rejected(self) -> None:
        client = make_client_with_responses([])
        client._conn.read_only = True  # type: ignore[attr-defined]
        for call in (
            lambda: client.down(),
            lambda: client.disable("all"),
            lambda: client.configure("/tmp/x.conf"),
            lambda: client.dump("routes", "/tmp/d.txt"),
            lambda: client.raw("restart all"),
        ):
            with pytest.raises(BirdValidationError, match="read-only"):
                call()
        assert client._conn.calls == []  # type: ignore[attr-defined]

    def test_configure_status_allowed(self) -> None:
        canned = b"0018-Configuration OK\n0000 \n"
        client = make_client_with_responses([canned])
        client._conn.read_only = True  # type: ignore[attr-defined]
        client.configure_status()
        assert client._conn.calls == ["configure status"]  # type: ignore[attr-defined]

    def test_real_client_read_only_flag(self) -> None:
        client = BirdClient(read_only=True)
        assert client.read_only is True
        assert client._conn._read_only is True  # type: ignore[attr-defined]


class TestClientComplexQueries:
    def test_show_route_query(self) -> None:
        client = make_client_with_responses([_ROUTE_OK])
        result = client.show_route_query(
            RouteQuery().table("master4").protocol("static_test").all_details()
        )
        assert client._conn.calls == [  # type: ignore[attr-defined]
            "show route table master4 all protocol static_test"
        ]
        assert result.entries[0].prefix == "192.0.2.0/24"

    def test_show_route_query_where_filter(self) -> None:
        client = make_client_with_responses([_ROUTE_OK])
        client.show_route_query(RouteQuery().where(RouteFilter.protocol("static_test")))
        assert client._conn.calls == [  # type: ignore[attr-defined]
            'show route where proto = "static_test"'
        ]

    def test_route_lookup_translates_network_not_found(self) -> None:
        canned = b"8001 Network not found\n"
        client = make_client_with_responses([canned])
        with pytest.raises(BirdCommandError):
            client.show_route_for("203.0.113.99")  # strict method still raises
        client2 = make_client_with_responses([canned])
        result = client2.route_lookup("203.0.113.99", table="master4")
        assert result.entries == []
        assert "Network not found" in result.raw_text

    def test_route_exists_false_on_network_not_found(self) -> None:
        client = make_client_with_responses([b"8001 Network not found\n"])
        assert client.route_exists("203.0.113.0/24") is False

    def test_route_exists_true(self) -> None:
        client = make_client_with_responses([_ROUTE_OK])
        assert client.route_exists("192.0.2.0/24") is True

    def test_count_routes_table(self) -> None:
        canned = (
            b"1007-6 of 6 routes for 6 networks in table master4\n"
            b"0014 Total: 6 of 6 routes for 6 networks in 2 tables\n"
        )
        client = make_client_with_responses([canned])
        assert client.count_routes(table="master4") == 6
        assert client._conn.calls == ["show route table master4 count"]  # type: ignore[attr-defined]

    def test_count_routes_all_tables(self) -> None:
        canned = (
            b"1007-6 of 6 routes for 6 networks in table master4\n"
            b" 0 of 0 routes for 0 networks in table master6\n"
            b"0014 Total: 6 of 6 routes for 6 networks in 2 tables\n"
        )
        client = make_client_with_responses([canned])
        assert client.count_routes() == 6
        assert client._conn.calls == ["show route table all count"]  # type: ignore[attr-defined]

    def test_get_protocol_found(self) -> None:
        canned = (
            b"2002-Name       Proto      Table      State  Since         Info\n"
            b"1002-static_test Static     master4    up     13:22:41.388  \n"
            b"1006-  Created:            13:22:41.388\n"
            b"0000 \n"
        )
        client = make_client_with_responses([canned])
        detail = client.get_protocol("static_test")
        assert detail is not None
        assert detail.name == "static_test"
        assert detail.created == "13:22:41.388"

    def test_get_protocol_not_found(self) -> None:
        # Unknown symbol -> daemon-side syntax error 9001 -> translated to None.
        client = make_client_with_responses([b"9001 syntax error, unexpected CF_SYM_UNDEFINED\n"])
        assert client.get_protocol("no_such_protocol") is None

    def test_get_protocol_other_error_reraised(self) -> None:
        # 8002 (not 8003, which now means "no match") must still propagate.
        client = make_client_with_responses([b"8002 something else\n"])
        with pytest.raises(BirdCommandError):
            client.get_protocol("static_test")


class TestNoMatchTranslations:
    """Pattern queries translate daemon-side 'No protocols match' (8003)
    into empty results instead of raising — mirroring route_lookup's
    handling of 8001."""

    def test_show_protocols_pattern_no_match(self) -> None:
        client = make_client_with_responses([b"8003 No protocols match\n"])
        result = client.show_protocols(pattern="zzz*")
        assert result.rows == []
        assert "No protocols match" in result.raw_text
        assert client._conn.calls == ['show protocols "zzz*"']  # type: ignore[attr-defined]

    def test_show_protocols_all_pattern_no_match(self) -> None:
        client = make_client_with_responses([b"8003 No protocols match\n"])
        result = client.show_protocols_all(pattern="zzz*")
        assert result.rows == []
        assert result.details == []

    def test_find_protocols_no_match(self) -> None:
        client = make_client_with_responses([b"8003 No protocols match\n"])
        assert client.find_protocols("zzz*").rows == []

    def test_bgp_sessions_pattern_no_match(self) -> None:
        client = make_client_with_responses([b"8003 No protocols match\n"])
        result = client.bgp_sessions(pattern="zzz*")
        assert result.sessions == []

    def test_get_protocol_no_match_pattern_form(self) -> None:
        # A quoted wildcard that matches nothing -> 8003 -> None.
        client = make_client_with_responses([b"8003 No protocols match\n"])
        assert client.get_protocol("zzz_*") is None

    def test_other_errors_still_raise(self) -> None:
        client = make_client_with_responses([b"8002 something else\n"])
        with pytest.raises(BirdCommandError):
            client.show_protocols(pattern="bgp*")
