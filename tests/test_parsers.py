"""Unit tests for the command-specific parsers.

Each test feeds the parser a captured fixture (see conftest.load_fixture_result)
or an inline response string, then asserts the structured fields.

Tests live next to the parser they exercise; the conftest at the package
root provides the shared :func:`load_fixture_result` helper.
"""

from __future__ import annotations

from conftest import load_fixture_result, make_result_from_text

from bird_api.models import RouteEntry
from bird_api.parsers import (
    parse_action,
    parse_bgp_sessions,
    parse_configure,
    parse_eval,
    parse_interfaces,
    parse_interfaces_summary,
    parse_memory,
    parse_protocols,
    parse_protocols_all,
    parse_route,
    parse_status,
    parse_symbols,
)


class TestStatusParser:
    def test_live_fixture(self) -> None:
        result = load_fixture_result("show_status", command="show status")
        status = parse_status(result)
        assert status.bird_version == "3.1.7"
        assert status.router_id == "10.0.0.1"
        assert status.hostname and status.hostname.startswith("c-6a8c3c11")
        assert status.current_server_time is not None
        assert status.last_reboot is not None
        assert status.last_reconfiguration is not None
        assert status.reconfig_state == "up"
        assert status.config_undo_in is None

    def test_reconfig_in_progress(self) -> None:
        # Synthetic fixture: daemon reports reconfiguration in progress.
        text = (
            "1000-BIRD 3.1.7\n"
            "1011-Router ID is 10.0.0.1\n"
            " Hostname is foo\n"
            "0013 Reconfiguration in progress\n"
        )
        result = make_result_from_text(text, command="show status")
        status = parse_status(result)
        assert status.reconfig_state == "reconfig"

    def test_shutdown_in_progress(self) -> None:
        text = "1000-BIRD 3.1.7\n0013 Shutdown in progress\n"
        result = make_result_from_text(text, command="show status")
        status = parse_status(result)
        assert status.reconfig_state == "shutdown"

    def test_raw_text_preserved(self) -> None:
        result = load_fixture_result("show_status", command="show status")
        status = parse_status(result)
        assert "BIRD 3.1.7" in status.raw_text
        assert "Router ID is 10.0.0.1" in status.raw_text
        assert "Daemon is up and running" in status.raw_text


class TestMemoryParser:
    def test_live_fixture(self) -> None:
        result = load_fixture_result("show_memory", command="show memory")
        mem = parse_memory(result)
        # We expect at least the major rows: Routing tables, Route attributes,
        # Protocols, Current config, Standby memory, Total.
        labels = {r.label for r in mem.rows}
        assert "Routing tables" in labels
        assert "Route attributes" in labels
        assert "Protocols" in labels
        assert "Current config" in labels
        assert "Standby memory" in labels
        assert "Total" in labels
        # The Total row should have both effective and overhead values.
        total = next(r for r in mem.rows if r.label == "Total")
        assert "kB" in total.effective
        assert "kB" in total.overhead

    def test_page_stats(self) -> None:
        result = load_fixture_result("show_memory", command="show memory")
        mem = parse_memory(result)
        assert "Active pages" in mem.page_stats
        assert "Kept free pages" in mem.page_stats
        assert "Cold free pages" in mem.page_stats

    def test_raw_text_preserved(self) -> None:
        result = load_fixture_result("show_memory", command="show memory")
        mem = parse_memory(result)
        assert "BIRD memory usage" in mem.raw_text
        assert "Effective" in mem.raw_text
        assert "Total:" in mem.raw_text


class TestProtocolsParser:
    def test_live_fixture(self) -> None:
        result = load_fixture_result("show_protocols", command="show protocols")
        p = parse_protocols(result)
        names = [r.name for r in p.rows]
        # The static_test, static_test2, device1, kernel1, direct1 are all in the test config.
        assert "static_test" in names
        assert "static_test2" in names
        assert "device1" in names
        assert "kernel1" in names
        assert "direct1" in names
        # All five should be in the "up" state in our test config.
        assert all(r.state == "up" for r in p.rows)

    def test_column_extraction(self) -> None:
        result = load_fixture_result("show_protocols", command="show protocols")
        p = parse_protocols(result)
        static = next(r for r in p.rows if r.name == "static_test")
        assert static.protocol == "Static"
        assert static.table == "master4"
        assert static.state == "up"
        # `since` is a short-form timestamp.
        assert ":" in static.since  # "13:22:41.388"

    def test_long_name_overflow(self) -> None:
        # "static_test" is 11 chars but the column width is 10. Make sure we
        # still parse the row correctly when the name overflows its column.
        text = (
            "2002-Name       Proto      Table      State  Since         Info\n"
            "1002-static_test Static     master4    up     13:22:41.388  \n"
            "0000 \n"
        )
        result = make_result_from_text(text, command="show protocols")
        p = parse_protocols(result)
        assert len(p.rows) == 1
        assert p.rows[0].name == "static_test"
        assert p.rows[0].protocol == "Static"
        assert p.rows[0].table == "master4"
        assert p.rows[0].state == "up"
        assert p.rows[0].since == "13:22:41.388"


class TestProtocolsAllParser:
    def test_live_fixture(self) -> None:
        result = load_fixture_result("show_protocols_all", command="show protocols all")
        pa = parse_protocols_all(result)
        # The summary rows should match the non-verbose version.
        assert len(pa.rows) == 5
        # Each row should have a corresponding detail block (with channels
        # for the protocols that have them).
        for row in pa.rows:
            detail = next(d for d in pa.details if d.name == row.name)
            assert detail.created is not None  # BIRD 3.x emits Created:
        # static_test has a channel with imported=3 routes.
        static_detail = next(d for d in pa.details if d.name == "static_test")
        assert len(static_detail.channels) == 1
        ch = static_detail.channels[0]
        assert ch.name == "ipv4"
        assert ch.routes_imported == 3
        assert ch.routes_exported == 0
        assert ch.routes_preferred == 3
        assert ch.preference == "200"

    def test_route_change_stats_parsed(self) -> None:
        result = load_fixture_result("show_protocols_all", command="show protocols all")
        pa = parse_protocols_all(result)
        kernel = next(d for d in pa.details if d.name == "kernel1")
        ch = kernel.channels[0]
        # The "Import updates" / "Export updates" rows should be parsed.
        assert "Import updates" in ch.route_change_stats
        assert "Export updates" in ch.route_change_stats
        # The headers should be on the channel's stat dict.
        imp = ch.route_change_stats["Import updates"]
        assert "received" in imp
        assert "accepted" in imp


class TestRouteParser:
    def test_live_fixture(self) -> None:
        result = load_fixture_result("show_route", command="show route")
        r = parse_route(result)
        assert r.table == "master4"
        assert len(r.entries) == 6
        # The kernel route should be the primary (*) with a via line.
        kernel = next(e for e in r.entries if e.protocol == "kernel1")
        assert kernel.primary is True
        assert len(kernel.via) == 1
        assert "via" in kernel.via[0]
        # The static routes should be marked non-primary (!) with no via.
        static = next(e for e in r.entries if e.protocol == "static_test")
        assert static.primary is False
        assert static.via == []

    def test_route_all_attributes(self) -> None:
        result = load_fixture_result("show_route_all", command="show route all")
        r = parse_route(result)
        # Each entry should have attribute_lines from the 1012 section.
        for e in r.entries:
            assert any("preference" in a for a in e.attribute_lines)
            assert any("source" in a for a in e.attribute_lines)
        # The kernel route should have extra krt_* attributes.
        kernel = next(e for e in r.entries if e.protocol == "kernel1")
        assert any("krt_source" in a for a in kernel.attribute_lines)
        assert any("krt_metric" in a for a in kernel.attribute_lines)

    def test_route_stats(self) -> None:
        result = load_fixture_result("show_route_stats", command="show route stats")
        r = parse_route(result)
        assert "routes" in r.stats
        assert r.stats["routes"] == "6"
        assert r.stats["routes_total"] == "6"
        assert r.stats["networks"] == "6"
        assert r.stats["tables"] == "2"
        # Per-table stats are also captured.
        assert r.stats["master4.routes"] == "6"
        assert r.stats["master6.routes"] == "0"

    def test_route_prefix_query(self) -> None:
        result = load_fixture_result("show_route_192.0.2.0_24", command="show route 192.0.2.0/24")
        r = parse_route(result)
        assert len(r.entries) == 1
        assert r.entries[0].prefix == "192.0.2.0/24"
        assert r.entries[0].destination == "blackhole"
        assert r.entries[0].protocol == "static_test"


class TestSymbolsParser:
    def test_live_fixture(self) -> None:
        result = load_fixture_result("show_symbols", command="show symbols")
        s = parse_symbols(result)
        # The fixture has hundreds of symbols; verify we got a non-trivial set.
        assert len(s.symbols) > 10
        classes = {sym.symbol_class for sym in s.symbols}
        assert "protocol" in classes
        assert "routing table" in classes
        assert "custom attribute" in classes
        # master4 and master6 routing tables should be present.
        tables = [sym.name for sym in s.symbols if sym.symbol_class == "routing table"]
        assert "master4" in tables
        assert "master6" in tables

    def test_tables_only(self) -> None:
        result = load_fixture_result("show_symbols_table", command="show symbols table")
        s = parse_symbols(result)
        assert len(s.symbols) == 2
        assert all(sym.symbol_class == "routing table" for sym in s.symbols)


class TestInterfacesParser:
    def test_live_fixture(self) -> None:
        result = load_fixture_result("show_interfaces", command="show interfaces")
        ifa = parse_interfaces(result)
        names = [i.name for i in ifa.interfaces]
        assert "lo" in names
        assert "dummy0" in names
        assert "eth0" in names
        # lo should have at least 2 addresses (v4 + v6).
        lo = next(i for i in ifa.interfaces if i.name == "lo")
        assert lo.admin_state == "up"
        assert len(lo.addresses) >= 2
        # dummy0 should be down with no addresses.
        dummy = next(i for i in ifa.interfaces if i.name == "dummy0")
        assert dummy.admin_state == "down"
        assert dummy.addresses == []
        # eth0 should be up with at least an IPv4 address.
        eth = next(i for i in ifa.interfaces if i.name == "eth0")
        assert eth.admin_state == "up"
        assert any("/" in a.address for a in eth.addresses)


class TestInterfacesSummaryParser:
    def test_live_fixture(self) -> None:
        result = load_fixture_result(
            "show_interfaces_summary",
            command="show interfaces summary",
        )
        ifas = parse_interfaces_summary(result)
        names = [r.name for r in ifas.rows]
        assert "lo" in names
        assert "eth0" in names
        lo = next(r for r in ifas.rows if r.name == "lo")
        assert lo.state == "up"
        assert lo.ipv4 == "127.0.0.1/8"
        assert lo.ipv6 == "::1/128"


class TestEvalParser:
    def test_live_fixture(self) -> None:
        result = load_fixture_result("eval_1p2", command="eval (1+2)")
        ev = parse_eval(result)
        assert ev.value == "3"


class TestConfigureParser:
    def test_configure_status(self) -> None:
        result = load_fixture_result("configure_status", command="configure status")
        c = parse_configure(result)
        assert c.file is None
        assert c.ok is True
        assert c.message == "Daemon is up and running"

    def test_configure_apply_success(self) -> None:
        text = (
            '0002-Reading configuration from /tmp/foo.conf\n'
            "0003 Reconfigured\n"
        )
        result = make_result_from_text(text, command='configure "/tmp/foo.conf"')
        c = parse_configure(result)
        assert c.file == "/tmp/foo.conf"
        assert c.ok is True
        assert c.message == "Reconfigured"
        assert c.undo_scheduled_in is None

    def test_configure_with_undo_timer(self) -> None:
        text = (
            '0002-Reading configuration from /tmp/foo.conf\n'
            "0022-Undo scheduled in 5 s\n"
            "0003 Reconfigured\n"
        )
        result = make_result_from_text(text, command='configure "/tmp/foo.conf" timeout 5')
        c = parse_configure(result)
        assert c.undo_scheduled_in == 5

    def test_configure_check_ok(self) -> None:
        text = (
            '0002-Reading configuration from /tmp/foo.conf\n'
            "0020 Configuration OK\n"
        )
        result = make_result_from_text(text, command='configure check "/tmp/foo.conf"')
        c = parse_configure(result)
        assert c.ok is True
        assert c.message == "Configuration OK"


class TestActionParser:
    def test_disable_action(self) -> None:
        text = "0009-direct1: disabled\n0000 \n"
        result = make_result_from_text(text, command="disable direct1")
        a = parse_action(result)
        assert a.per_target == ["direct1: disabled"]
        assert a.message == ""

    def test_enable_all_action(self) -> None:
        text = (
            "0010-device1: already enabled\n"
            "0009-direct1: enabled\n"
            "0000 \n"
        )
        result = make_result_from_text(text, command="enable all")
        a = parse_action(result)
        assert len(a.per_target) == 2
        assert "device1: already enabled" in a.per_target
        assert "direct1: enabled" in a.per_target

    def test_dump_action(self) -> None:
        text = (
            "0013-Dumping resources to /tmp/dump.txt\n"
            " ... dumped 46997 bytes in 0.000 s\n"
            "0013 Dumped 46997 bytes in 0.000 s\n"
        )
        result = make_result_from_text(text, command='dump resources "/tmp/dump.txt"')
        a = parse_action(result)
        # The first line is a continuation of code 13; the final is the same code.
        # We pick up both per-target lines.
        assert any("Dumping resources" in line for line in a.per_target)
        assert a.message == "Dumped 46997 bytes in 0.000 s"

    def test_error_response_raises_in_connection_layer(self) -> None:
        # The connection layer raises BirdCommandError for final codes >= 8000,
        # so the parser never sees them. Verify that we DO see them when we
        # construct a result directly (e.g. when the raw_text is the error).
        text = "8003 No protocols match\n"
        result = make_result_from_text(text, command="disable nonexistent")
        assert not result.success
        assert result.error_message == "No protocols match"


# ---------------------------------------------------------------------------
# BGP session parsing (show protocols all on a BGP daemon)
# ---------------------------------------------------------------------------


class TestBgpSessions:
    def test_parse_established_session(self) -> None:
        result = load_fixture_result("bgp_protocols_all", command="show protocols all")
        parsed = parse_protocols_all(result)
        sessions = parse_bgp_sessions(parsed.details, raw_text=result.raw_text, command="show protocols all")

        assert len(sessions.sessions) == 1
        s = sessions.sessions[0]
        assert s.name == "peer_b"
        assert s.state == "Established"
        assert s.established
        assert s.neighbor_address == "127.0.0.3"
        assert s.neighbor_port == 1179
        assert s.neighbor_as == 65000
        assert s.local_as == 65000
        assert s.neighbor_id == "10.0.0.2"
        assert s.session == "internal multihop AS4"
        assert s.source_address == "127.0.0.2"
        assert s.hold_timer is not None and "/" in s.hold_timer
        assert s.keepalive_timer is not None and "/" in s.keepalive_timer
        assert s.send_hold_timer is not None

    def test_capabilities_collected(self) -> None:
        result = load_fixture_result("bgp_protocols_all", command="show protocols all")
        parsed = parse_protocols_all(result)
        sessions = parse_bgp_sessions(parsed.details, raw_text=result.raw_text, command="show protocols all")
        caps = sessions.sessions[0].capabilities
        assert "Local capabilities" in caps
        assert "Neighbor capabilities" in caps
        assert any("Multiprotocol" in c for c in caps)
        assert any("AF announced: ipv4" in c for c in caps)
        assert any("4-octet AS numbers" in c for c in caps)

    def test_non_bgp_daemon_yields_empty(self) -> None:
        result = load_fixture_result("show_protocols_all", command="show protocols all")
        parsed = parse_protocols_all(result)
        sessions = parse_bgp_sessions(parsed.details, raw_text=result.raw_text, command="show protocols all")
        assert sessions.sessions == []

    def test_raw_text_preserved(self) -> None:
        result = load_fixture_result("bgp_protocols_all", command="show protocols all")
        parsed = parse_protocols_all(result)
        sessions = parse_bgp_sessions(parsed.details, raw_text=result.raw_text, command="show protocols all")
        assert "Established" in sessions.raw_text
        assert sessions.command == "show protocols all"


class TestBgpRouteParsing:
    """BGP-learned route lines exercise the extended route-line grammar."""

    def test_bgp_route_line(self) -> None:
        result = load_fixture_result("bgp_route_all", command="show route all")
        parsed = parse_route(result)

        bgp = [e for e in parsed.entries if e.protocol == "peer_b"]
        assert len(bgp) == 1
        entry = bgp[0]
        assert entry.prefix == "198.51.100.0/24"
        assert entry.destination == "unreachable"
        assert entry.from_ip == "127.0.0.3"
        assert entry.primary
        assert not entry.sync_error
        assert entry.preference == 100
        assert entry.info == "(100) [i]"

    def test_bgp_attributes(self) -> None:
        result = load_fixture_result("bgp_route_all", command="show route all")
        parsed = parse_route(result)
        entry = [e for e in parsed.entries if e.protocol == "peer_b"][0]
        attrs = entry.attributes
        assert attrs["bgp_origin"] == "IGP"
        assert attrs["bgp_next_hop"] == "127.0.0.3"
        assert attrs["bgp_local_pref"] == "100"
        assert attrs["from"] == "127.0.0.3"
        assert attrs["source"] == "BGP"
        assert attrs["preference"] == "100"

    def test_static_route_still_parses(self) -> None:
        result = load_fixture_result("bgp_route_all", command="show route all")
        parsed = parse_route(result)
        entry = [e for e in parsed.entries if e.protocol == "stat_a"][0]
        assert entry.prefix == "192.0.2.0/24"
        assert entry.destination == "blackhole"
        assert entry.from_ip is None
        assert entry.preference == 200
        assert entry.info == "(200)"
        assert entry.attributes == {"preference": "200", "source": "static"}

    def test_non_primary_route_line(self) -> None:
        # A second candidate route carries no '*'/'!' marker at all.
        text = (
            "1007-Table master4:\n"
            " 10.0.0.0/8           unicast [proto_a 13:22:41.388] * (100)\n"
            " 10.0.0.0/8           unicast [proto_b 13:22:41.388] (100)\n"
            "0000 \n"
        )
        parsed = parse_route(make_result_from_text(text, command="show route all"))
        assert len(parsed.entries) == 2
        assert parsed.entries[0].primary
        assert not parsed.entries[1].primary
        assert not parsed.entries[1].sync_error

    def test_attribute_continuation_lines(self) -> None:
        # Long community lists wrap; continuation lines have no "key: " shape.
        entry = RouteEntry(
            prefix="10.0.0.0/8",
            destination="unicast",
            protocol="bgp1",
            timestamp="t",
            primary=True,
            via=[],
            dev=[],
            attribute_lines=[
                "bgp_community: (65000,1) (65000,2)",
                "(65000,3) (65000,4)",
            ],
            raw_lines=[],
        )
        attrs = entry.attributes
        assert attrs["bgp_community"] == "(65000,1) (65000,2) (65000,3) (65000,4)"


class TestPatternQueries:
    def test_pattern_response(self) -> None:
        result = load_fixture_result("show_protocols_pattern", command='show protocols "static*"')
        parsed = parse_protocols(result)
        assert len(parsed.rows) == 2
        assert {r.name for r in parsed.rows} == {"static_test", "static_test2"}

    def test_single_protocol_detail(self) -> None:
        result = load_fixture_result("show_protocols_all_single", command="show protocols all static_test")
        parsed = parse_protocols_all(result)
        assert len(parsed.details) == 1
        assert parsed.details[0].name == "static_test"
        assert parsed.details[0].channels[0].routes_imported == 3

    def test_where_proto_response(self) -> None:
        result = load_fixture_result(
            "show_route_where_proto", command='show route where proto = "static_test"'
        )
        parsed = parse_route(result)
        assert len(parsed.entries) == 3
        assert all(e.protocol == "static_test" for e in parsed.entries)
