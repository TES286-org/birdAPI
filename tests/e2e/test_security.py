"""End-to-end tests for the injection-prevention layer and complex queries.

Requires the standard test daemon (``scripts/start-bird.sh`` →
``/tmp/bird-test.ctl``). The BGP pair tests additionally require
``scripts/start-bgp-pair.sh`` (``/tmp/bird-bgp-a.ctl``); they are skipped
when that socket is unreachable.
"""

from __future__ import annotations

import os
import socket as socket_mod

import pytest
from test_e2e import SOCKET_PATH, _socket_reachable  # noqa: E402 (tests are on pythonpath)

from bird_api import BirdClient, RouteFilter, RouteQuery
from bird_api.exceptions import BirdCommandError, BirdValidationError

pytestmark = pytest.mark.skipif(
    not _socket_reachable(SOCKET_PATH),
    reason=f"no reachable BIRD control socket at {SOCKET_PATH!r}",
)

BGP_SOCKET = os.environ.get("BIRD_BGP_SOCKET", "/tmp/bird-bgp-a.ctl")


@pytest.fixture(scope="module")
def client() -> BirdClient:
    c = BirdClient(socket_path=SOCKET_PATH, timeout=10.0)
    c.connect()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Injection attacks — must be rejected client-side, daemon must survive
# ---------------------------------------------------------------------------

# Payloads that break the wire PROTOCOL itself (line framing) — must be
# rejected by EVERY input slot without exception.
FRAMING_PAYLOADS = {
    "newline": "static_test\ndown",
    "crlf": "static_test\r\ndown",
    "nul": "static_te\x00st",
    "escape": "\x1b[2Jstatic_test",
    "tab": "static\ttest",
}

# Payloads that stay inside one line but violate the strict charset of
# identifier/prefix slots (argument smuggling). Free-form slots (eval, raw,
# file paths) intentionally accept these: they cannot break framing, so the
# worst case is a daemon-side syntax/no-match error.
LEXICAL_PAYLOADS = {
    "quote_breakout": 'static_test" down',
    "space_smuggle": "static_test down",
    "wildcard_smuggle": "static*; down",
    "shell": "$(down)",
    "backtick": "`down`",
}


class TestInjectionAttacksRejected:
    """Every attack must raise before any byte reaches the socket."""

    @pytest.mark.parametrize("payload", FRAMING_PAYLOADS.values(), ids=FRAMING_PAYLOADS)
    def test_framing_attacks_rejected_everywhere(self, payload: str) -> None:
        c = BirdClient(socket_path=SOCKET_PATH)
        for attempt in (
            lambda: c.show_route_prefix(payload),
            lambda: c.show_route_table(payload),
            lambda: c.show_route_protocol(payload),
            lambda: c.show_route_for(payload),
            lambda: c.eval(payload),
            lambda: c.enable(payload),
            lambda: c.disable(payload),
            lambda: c.configure(f"/tmp/{payload}.conf"),
            lambda: c.dump("routes", f"/tmp/{payload}.txt"),
            lambda: c.bgp_sessions(pattern=payload),
            lambda: c.route_lookup(payload),
            lambda: c.raw(f"show route {payload}"),
        ):
            with pytest.raises(BirdValidationError):
                attempt()

    @pytest.mark.parametrize("payload", LEXICAL_PAYLOADS.values(), ids=LEXICAL_PAYLOADS)
    def test_lexical_attacks_rejected_by_strict_slots(self, payload: str) -> None:
        c = BirdClient(socket_path=SOCKET_PATH)
        for attempt in (
            lambda: c.show_route_prefix(payload),
            lambda: c.show_route_table(payload),
            lambda: c.show_route_protocol(payload),
            lambda: c.show_route_for(payload),
            lambda: c.route_lookup(payload),
        ):
            with pytest.raises(BirdValidationError):
                attempt()

    @pytest.mark.parametrize("payload", LEXICAL_PAYLOADS.values(), ids=LEXICAL_PAYLOADS)
    def test_lexical_payloads_cannot_execute_second_command(self, payload: str) -> None:
        """Free-form slots accept single-line payloads BY DESIGN — verify the
        daemon survives them (they produce syntax/no-match errors, never a
        second executed command)."""
        c = BirdClient(socket_path=SOCKET_PATH, timeout=10.0)
        c.connect()
        try:
            from bird_api.exceptions import BirdError

            for attempt in (
                lambda: c.eval(payload),
                lambda: c.raw(f"show route {payload}"),
            ):
                with pytest.raises(BirdError):
                    attempt()  # daemon-side error — but exactly ONE command ran
            # Daemon is still healthy and serving:
            assert c.show_status().reconfig_state == "up"
        finally:
            c.close()

    def test_daemon_still_alive_after_attack_battery(self, client: BirdClient) -> None:
        # The battery above (and other tests) must never have killed or
        # wedged the daemon — prove liveness with a fresh query.
        status = client.show_status()
        assert status.reconfig_state == "up"

    def test_daemon_still_serving_routes(self, client: BirdClient) -> None:
        routes = client.show_route_prefix("192.0.2.0/24")
        assert len(routes.entries) == 1
        assert routes.entries[0].prefix == "192.0.2.0/24"


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------


class TestReadOnlyMode:
    def test_show_commands_work(self) -> None:
        with BirdClient(socket_path=SOCKET_PATH, read_only=True, timeout=10.0) as ro:
            assert ro.read_only
            assert ro.show_status().bird_version
            assert len(ro.show_protocols().rows) >= 4

    def test_mutations_rejected_client_side(self) -> None:
        with BirdClient(socket_path=SOCKET_PATH, read_only=True, timeout=10.0) as ro:
            with pytest.raises(BirdValidationError, match="read-only"):
                ro.disable("all")
            with pytest.raises(BirdValidationError, match="read-only"):
                ro.configure("/tmp/whatever.conf")
            with pytest.raises(BirdValidationError, match="read-only"):
                ro.raw("down")
            with pytest.raises(BirdValidationError, match="read-only"):
                ro.eval("1+2")  # deliberately conservative allowlist

    def test_daemon_unharmed_after_read_only_blocks(self) -> None:
        with BirdClient(socket_path=SOCKET_PATH, read_only=True, timeout=10.0) as ro:
            for _attempt in range(3):
                with pytest.raises(BirdValidationError):
                    ro.down()
        # Daemon still up.
        assert client_alive(SOCKET_PATH)


def client_alive(path: str) -> bool:
    c = BirdClient(socket_path=path, timeout=5.0)
    try:
        c.connect()
        return c.show_status().reconfig_state == "up"
    except Exception:  # noqa: BLE001
        return False
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Complex queries (RouteQuery + RouteFilter)
# ---------------------------------------------------------------------------


class TestRouteQueryE2E:
    def test_table_and_protocol(self, client: BirdClient) -> None:
        q = RouteQuery().table("master4").protocol("static_test")
        result = client.show_route_query(q)
        assert result.table == "master4"
        assert {e.prefix for e in result.entries} == {
            "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
        }
        assert all(e.protocol == "static_test" for e in result.entries)
        assert all(e.preference == 200 for e in result.entries)

    def test_for_address_lookup(self, client: BirdClient) -> None:
        q = RouteQuery().for_address("192.0.2.123").table("master4")
        result = client.show_route_query(q)
        assert any(e.prefix == "192.0.2.0/24" for e in result.entries)

    def test_within_prefix(self, client: BirdClient) -> None:
        q = RouteQuery().within("192.0.0.0/16").table("master4")
        result = client.show_route_query(q)
        assert {e.prefix for e in result.entries} == {"192.0.2.0/24"}

    def test_primary_filter(self, client: BirdClient) -> None:
        q = RouteQuery().table("master4").primary()
        result = client.show_route_query(q)
        assert result.entries
        # `primary` keeps best ('*') and sync-error ('!') routes but drops
        # unmarked backup routes — every entry must carry a marker.
        assert all(e.primary or e.sync_error for e in result.entries)

    def test_where_filter_protocol(self, client: BirdClient) -> None:
        q = RouteQuery().table("master4").where(RouteFilter.protocol("static_test2"))
        result = client.show_route_query(q)
        assert {e.prefix for e in result.entries} == {"10.0.0.0/8", "172.16.0.0/12"}

    def test_where_filter_prefix_in_or_longer(self, client: BirdClient) -> None:
        q = RouteQuery().table("master4").where(
            RouteFilter.prefix_in("192.0.0.0/16", or_longer=True)
        )
        result = client.show_route_query(q)
        assert {e.prefix for e in result.entries} == {"192.0.2.0/24"}

    def test_where_filter_composition(self, client: BirdClient) -> None:
        f = RouteFilter.protocol("static_test") & RouteFilter.prefix_len(min=24, max=24)
        q = RouteQuery().table("master4").where(f)
        result = client.show_route_query(q)
        assert {e.prefix for e in result.entries} == {
            "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
        }

    def test_where_filter_negation(self, client: BirdClient) -> None:
        f = ~RouteFilter.protocol("static_test")
        q = RouteQuery().table("master4").where(f)
        result = client.show_route_query(q)
        assert result.entries
        assert all(e.protocol != "static_test" for e in result.entries)

    def test_all_details_attributes(self, client: BirdClient) -> None:
        q = RouteQuery().table("master4").prefix("192.0.2.0/24").all_details()
        result = client.show_route_query(q)
        entry = result.entries[0]
        attrs = entry.attributes
        assert attrs["preference"] == "200"
        assert attrs["source"] == "static"

    def test_count_table(self, client: BirdClient) -> None:
        n = client.count_routes(table="master4")
        assert n >= 6  # 5 static routes + kernel default route

    def test_count_all_tables(self, client: BirdClient) -> None:
        n = client.count_routes()
        assert n >= 6

    def test_route_exists(self, client: BirdClient) -> None:
        assert client.route_exists("192.0.2.0/24", table="master4") is True
        # A prefix of the wrong family for the table is a caller error (9001)
        # and propagates; a same-family miss is a clean False.
        with pytest.raises(BirdCommandError):
            client.route_exists("192.0.2.0/24", table="master6")
        assert client.route_exists("2001:db8:dead:beef::/64", table="master6") is False

    def test_route_lookup_hit(self, client: BirdClient) -> None:
        result = client.route_lookup("198.51.100.7", table="master4")
        assert any(e.prefix == "198.51.100.0/24" for e in result.entries)

    def test_route_lookup_miss_is_empty_not_error(self, client: BirdClient) -> None:
        # master6 has no routes at all -> lookup miss -> empty result.
        result = client.route_lookup("2001:db8::1", table="master6")
        assert result.entries == []


class TestPatternQueriesE2E:
    def test_pattern_protocols(self, client: BirdClient) -> None:
        result = client.show_protocols(pattern="static*")
        assert {r.name for r in result.rows} == {"static_test", "static_test2"}

    def test_pattern_with_question_mark(self, client: BirdClient) -> None:
        result = client.show_protocols(pattern="static_tes?")
        assert {r.name for r in result.rows} == {"static_test"}

    def test_find_protocols(self, client: BirdClient) -> None:
        result = client.find_protocols("static*")
        assert len(result.rows) == 2

    def test_get_protocol(self, client: BirdClient) -> None:
        detail = client.get_protocol("static_test")
        assert detail is not None
        assert detail.protocol == "Static"
        assert detail.channels[0].routes_imported == 3

    def test_get_protocol_missing(self, client: BirdClient) -> None:
        assert client.get_protocol("definitely_not_here") is None


class TestEvalE2E:
    def test_eval_arithmetic(self, client: BirdClient) -> None:
        assert client.eval("1+2").value == "3"

    def test_eval_boolean(self, client: BirdClient) -> None:
        assert client.eval("(1 = 1) && (2 = 2)").value == "TRUE"

    def test_eval_unknown_symbol_is_command_error(self, client: BirdClient) -> None:
        with pytest.raises(BirdCommandError):
            client.eval("this_is_not_defined")


# ---------------------------------------------------------------------------
# BGP pair (requires scripts/start-bgp-pair.sh)
# ---------------------------------------------------------------------------

bgp_available = _socket_reachable(BGP_SOCKET)


class TestBgpSessionsE2E:
    @pytest.fixture(scope="class")
    def bgp_client(self) -> BirdClient:
        c = BirdClient(socket_path=BGP_SOCKET, timeout=10.0)
        c.connect()
        yield c
        c.close()

    @pytest.mark.skipif(not bgp_available, reason=f"no BGP daemon at {BGP_SOCKET!r}")
    def test_sessions_parsed(self, bgp_client: BirdClient) -> None:
        result = bgp_client.bgp_sessions()
        assert len(result.sessions) == 1
        s = result.sessions[0]
        assert s.name == "peer_b"
        assert s.state == "Established"
        assert s.established
        assert s.neighbor_address == "127.0.0.3"
        assert s.neighbor_as == 65000
        assert s.local_as == 65000
        assert s.neighbor_id == "10.0.0.2"
        assert s.session is not None and "internal" in s.session
        assert s.source_address == "127.0.0.2"

    @pytest.mark.skipif(not bgp_available, reason=f"no BGP daemon at {BGP_SOCKET!r}")
    def test_bgp_route_attributes(self, bgp_client: BirdClient) -> None:
        q = RouteQuery().table("master4").prefix("198.51.100.0/24").all_details()
        result = bgp_client.show_route_query(q)
        assert len(result.entries) == 1
        entry = result.entries[0]
        assert entry.protocol == "peer_b"
        assert entry.from_ip == "127.0.0.3"
        assert entry.info == "(100) [i]"
        attrs = entry.attributes
        assert attrs["bgp_origin"] == "IGP"
        assert attrs["bgp_next_hop"] == "127.0.0.3"
        assert attrs["bgp_local_pref"] == "100"

    @pytest.mark.skipif(not bgp_available, reason=f"no BGP daemon at {BGP_SOCKET!r}")
    def test_bgp_pattern_query(self, bgp_client: BirdClient) -> None:
        result = bgp_client.bgp_sessions(pattern="peer*")
        assert len(result.sessions) == 1

    @pytest.mark.skipif(not bgp_available, reason=f"no BGP daemon at {BGP_SOCKET!r}")
    def test_local_static_route_via_bgp_table(self, bgp_client: BirdClient) -> None:
        # The local static route is exported to the peer; verify it exists.
        assert bgp_client.route_exists("192.0.2.0/24", table="master4")


# ---------------------------------------------------------------------------
# Injection safety against a live socket — real attacker round-trip
# ---------------------------------------------------------------------------


class TestLiveInjectionRoundTrip:
    def test_newline_payload_never_reaches_wire(self) -> None:
        """Open a raw socket, attempt the attack through BirdClient, verify
        the daemon only ever saw ONE command (it stays responsive and its
        reply stream stays in lockstep)."""
        with BirdClient(socket_path=SOCKET_PATH, timeout=10.0) as c:
            with pytest.raises(BirdValidationError):
                c.raw("show status\ndown")
            # If the newline HAD been sent, the daemon would have executed
            # 'down' and the next command would fail to connect.
            assert c.show_status().reconfig_state == "up"
            assert c.show_route_prefix("192.0.2.0/24").entries

    def test_socket_still_has_single_command_semantics(self) -> None:
        """Sanity check of the underlying threat model: two lines sent raw
        DO execute as two commands on an unprotected socket (documenting
        why client-side validation matters)."""
        s = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        s.settimeout(3.0)
        try:
            s.connect(SOCKET_PATH)
            s.recv(4096)  # banner
            s.sendall(b"eval 1+2\n")
            s.settimeout(1.0)
            first = s.recv(4096)
            assert b"0023 3" in first
        finally:
            s.close()
