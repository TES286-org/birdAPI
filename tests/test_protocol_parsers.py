"""Tests for the protocol-specific parsers (OSPF / BFD / RIP / Babel / static / MPLS).

Real captured fixtures cover the shapes the live daemons produce in this
environment (empty neighbor tables, stub OSPF interface, established BFD
session). Row-populated shapes are exercised with synthetic wire data
assembled strictly from the daemon's printf format strings (referenced per
parser module), since the sandbox cannot form OSPF/RIP/Babel adjacencies.
"""

from __future__ import annotations

from conftest import load_fixture_result, make_result_from_text

from bird_api.parsers import (
    parse_babel_interfaces,
    parse_babel_neighbors,
    parse_bfd_sessions,
    parse_mpls_ranges,
    parse_ospf,
    parse_ospf_interfaces,
    parse_ospf_lsadb,
    parse_ospf_neighbors,
    parse_ospf_state,
    parse_rip_interfaces,
    parse_rip_neighbors,
    parse_static,
)

# ---------------------------------------------------------------------------
# Real fixtures
# ---------------------------------------------------------------------------


class TestStaticParser:
    def test_blackhole_route(self) -> None:
        result = parse_static(load_fixture_result("show_static"))
        assert len(result.routes) == 1
        route = result.routes[0]
        assert route.prefix == "192.0.2.0/24"
        assert route.dest_type == "blackhole"
        assert route.nexthops == []
        assert not route.dormant

    def test_unicast_with_nexthops(self) -> None:
        text = (
            "1009-10.0.0.0/8\n"
            " \tvia 192.0.2.1 on eth0\n"
            " \tvia 192.0.2.2 on eth0 weight 2\n"
            "0000 \n"
        )
        result = parse_static(make_result_from_text(text, command="show static"))
        assert len(result.routes) == 1
        route = result.routes[0]
        assert route.dest_type == "unicast"
        assert len(route.nexthops) == 2
        assert route.nexthops[0] == "via 192.0.2.1 on eth0"
        assert "weight 2" in route.nexthops[1]
        assert not route.dormant

    def test_dormant_and_bfd_nexthops(self) -> None:
        text = (
            "1009-172.16.0.0/12\n"
            " \tvia 192.0.2.1 on eth0 (bfd)\n"
            " \tdev eth0 (dormant)\n"
            "1009-198.51.100.0/24\tunreachable\n"
            "1009-203.0.113.0/24\trecursive 192.0.2.1\n"
            "0000 \n"
        )
        result = parse_static(make_result_from_text(text))
        assert len(result.routes) == 3
        first = result.routes[0]
        assert first.dormant  # second nexthop carries (dormant)
        assert first.nexthops[0] == "via 192.0.2.1 on eth0 (bfd)"
        assert result.routes[1].dest_type == "unreachable"
        rec = result.routes[2]
        assert rec.dest_type == "recursive"
        assert rec.nexthops == ["192.0.2.1"]

    def test_raw_text_preserved(self) -> None:
        result = parse_static(load_fixture_result("show_static"))
        assert "192.0.2.0/24" in result.raw_text


class TestOspfParser:
    def test_summary_from_live_fixture(self) -> None:
        result = parse_ospf(load_fixture_result("show_ospf"))
        assert result.protocol == "ospf_a"
        assert result.up
        assert result.rfc1583 is False
        assert result.stub_router is False
        assert result.tick == 1
        assert result.num_areas == 1
        assert result.num_lsas == 1
        assert len(result.areas) == 1
        area = result.areas[0]
        assert area.area_id == "0.0.0.0"
        assert area.backbone
        assert area.stub is False
        assert area.nssa is False
        assert area.transit is False
        assert area.interfaces_count == 1
        assert area.neighbors_count == 0
        assert area.adjacent_count == 0

    def test_not_up(self) -> None:
        text = "1014-ospf_a: is not up\n0000 \n"
        result = parse_ospf(make_result_from_text(text))
        assert result.protocol == "ospf_a"
        assert not result.up
        assert result.areas == []

    def test_neighbors_empty_live(self) -> None:
        result = parse_ospf_neighbors(load_fixture_result("show_ospf_neighbors"))
        assert result.protocol == "ospf_a"
        assert result.up
        assert result.rows == []

    def test_neighbors_rows_synthetic(self) -> None:
        # "%-12R\t%3u\t%s/%s\t%6t\t%-10s %I" (proto/ospf/neighbor.c)
        text = (
            "1013-ospf_a:\n"
            " Router ID   \tPri\t     State     \tDTime\tInterface  Router IP\n"
            " 10.0.0.2   \t  1\tFull/PtP     \t   32\teth0       21.0.22.68\n"
            " 10.0.0.3   \t  5\tFull/DR      \t    2\teth1       21.0.22.69\n"
            "0000 \n"
        )
        result = parse_ospf_neighbors(make_result_from_text(text))
        assert len(result.rows) == 2
        row = result.rows[0]
        assert row.router_id == "10.0.0.2"
        assert row.priority == 1
        assert row.state == "Full"
        assert row.position == "PtP"
        assert row.dead_timer == "32"
        assert row.interface == "eth0"
        assert row.router_ip == "21.0.22.68"
        assert result.rows[1].position == "DR"
        assert result.rows[1].priority == 5

    def test_interfaces_live(self) -> None:
        result = parse_ospf_interfaces(load_fixture_result("show_ospf_interface"))
        assert result.protocol == "ospf_a"
        assert result.up
        assert len(result.interfaces) == 1
        iface = result.interfaces[0]
        assert iface.name == "eth0"
        assert iface.header == "Interface eth0 (21.0.22.68/32)"
        assert iface.type == "ptp"
        assert iface.area == "0.0.0.0 (0)"
        assert iface.state == "PtP (stub)"
        assert iface.cost == 10
        assert iface.fields["Hello timer"] == "2"
        assert iface.fields["Dead timer"] == "8"

    def test_state_live(self) -> None:
        result = parse_ospf_state(load_fixture_result("show_ospf_state"))
        assert result.up
        assert result.areas == ["area 0.0.0.0"]
        assert result.routers == ["router 10.0.0.1"]
        assert any("stubnet 21.0.22.68/32 metric 0" in ln for ln in result.lines)

    def test_lsadb_live(self) -> None:
        result = parse_ospf_lsadb(load_fixture_result("show_ospf_lsadb"))
        assert result.up
        assert len(result.scopes) == 1
        scope = result.scopes[0]
        assert scope.scope == "Area 0.0.0.0"
        assert len(scope.rows) == 1
        row = scope.rows[0]
        assert row.type == "0001"
        assert row.ls_id == "10.0.0.1"
        assert row.router == "10.0.0.1"
        assert row.sequence == "80000001"
        assert isinstance(row.age, int)
        assert row.checksum == "1f6e"
        assert result.rows == scope.rows

    def test_lsadb_multiple_scopes_synthetic(self) -> None:
        text = (
            "1017-\n"
            " Global\n"
            " \n"
            "  Type   LS ID           Router          Sequence   Age  Checksum\n"
            " 0005  0.0.0.0          10.0.0.1         80000002    10    2a3b\n"
            " \n"
            " Area 0.0.0.1\n"
            " \n"
            "  Type   LS ID           Router          Sequence   Age  Checksum\n"
            " 0001  10.0.0.2        10.0.0.2         80000001     5    cd2f\n"
            "0000 \n"
        )
        result = parse_ospf_lsadb(make_result_from_text(text))
        assert [s.scope for s in result.scopes] == ["Global", "Area 0.0.0.1"]
        assert result.scopes[0].rows[0].type == "0005"
        assert result.scopes[1].rows[0].ls_id == "10.0.0.2"
        assert len(result.rows) == 2


class TestBfdParser:
    def test_table_live(self) -> None:
        result = parse_bfd_sessions(load_fixture_result("show_bfd_sessions"))
        assert result.protocols == ["bfd_test"]
        assert result.up
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.address == "127.0.0.3"
        assert row.interface == "---"
        assert row.state == "Up"
        assert row.since  # timestamp
        assert row.interval == "0.100"
        assert row.timeout == "0.500"
        assert not result.verbose

    def test_verbose_live(self) -> None:
        result = parse_bfd_sessions(load_fixture_result("show_bfd_sessions_all"))
        assert result.protocols == ["bfd_test"]
        assert result.details and not result.rows
        assert result.verbose
        detail = result.details[0]
        assert detail.address == "127.0.0.3"
        assert detail.interface == "---"
        assert detail.session_type == "Multihop"
        assert detail.state == "Up"
        assert detail.remote_state == "Up"
        assert detail.fields["Local diagnostic"] == "None"
        assert detail.fields["Local discriminator"] == "2370786090"
        assert detail.local_params["Min TX interval"] == "0.100"
        assert detail.local_params["Multiplier"] == "5"
        assert detail.remote_params["Min RX interval"] == "0.010"

    def test_multiple_verbose_sessions(self) -> None:
        block = (
            "   Address:              127.0.0.3\n"
            "   Interface:            ---\n"
            "   Session state:        Up\n"
            "   Local parameters:\n"
            "     Min TX interval:    0.100\n"
            " \n"
        )
        text = "1020-bfd_test:\n" + block + block + "0000 \n"
        result = parse_bfd_sessions(make_result_from_text(text))
        assert len(result.details) == 2
        assert result.details[0].address == "127.0.0.3"
        assert result.details[1].address == "127.0.0.3"

    def test_not_up(self) -> None:
        text = "1020-bfd_test: is not up\n0000 \n"
        result = parse_bfd_sessions(make_result_from_text(text))
        assert not result.up
        assert result.protocols == ["bfd_test"]


class TestRipParser:
    def test_interfaces_live(self) -> None:
        result = parse_rip_interfaces(load_fixture_result("show_rip_interfaces"))
        assert result.protocol == "rip_test"
        assert result.up
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.interface == "eth0"
        assert row.state == "Up"
        assert row.metric == 1
        assert row.neighbors == 0

    def test_neighbors_rows_synthetic(self) -> None:
        # "%-25I %-10s %6u %6u %7t" (proto/rip/rip.c)
        text = (
            "1022-rip_test:\n"
            " IP address                Interface  Metric Routes    Seen\n"
            " 192.0.2.1                 eth0            1      3     12\n"
            " 198.51.100.1              eth1            2      0      4\n"
            "0000 \n"
        )
        result = parse_rip_neighbors(make_result_from_text(text))
        assert len(result.rows) == 2
        row = result.rows[0]
        assert row.address == "192.0.2.1"
        assert row.interface == "eth0"
        assert row.metric == 1
        assert row.routes == 3
        assert row.seen == "12"

    def test_not_up(self) -> None:
        text = "1021-rip_test: is not up\n0000 \n"
        result = parse_rip_interfaces(make_result_from_text(text))
        assert not result.up
        assert result.rows == []


class TestBabelParser:
    def test_interfaces_live(self) -> None:
        result = parse_babel_interfaces(load_fixture_result("show_babel_interfaces"))
        assert result.protocol == "babel_test"
        assert result.up
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.interface == "eth0"
        assert row.state == "Up"
        assert row.auth == "No"
        assert row.rxcost == 96
        assert row.neighbors == 0
        assert row.next_hop_v4 == "21.0.22.68"
        assert row.next_hop_v6.startswith("fe80::")

    def test_neighbors_rows_synthetic(self) -> None:
        # "%-25I %-10s %6u %6u %6u %7t %-4s %9t" (proto/babel/babel.c)
        text = (
            "1024-babel_test:\n"
            " IP address                Interface  Metric Routes Hellos Expires Auth  RTT (ms)\n"
            " fe80::3452:3bff:fe15:9910 eth0          96      3      3      9 No            3.4\n"
            "0000 \n"
        )
        result = parse_babel_neighbors(make_result_from_text(text))
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.address == "fe80::3452:3bff:fe15:9910"
        assert row.interface == "eth0"
        assert row.metric == 96
        assert row.routes == 3
        assert row.hellos == 3
        assert row.expires == "9"
        assert row.auth == "No"
        assert row.rtt_ms == "3.4"

    def test_not_up(self) -> None:
        text = "1023-babel_test: is not up\n0000 \n"
        result = parse_babel_interfaces(make_result_from_text(text))
        assert not result.up


class TestMplsParser:
    def test_live_ranges(self) -> None:
        result = parse_mpls_ranges(load_fixture_result("show_mpls_ranges"))
        assert len(result.domains) == 1
        domain = result.domains[0]
        assert domain.domain == "test_dom"
        names = [r.name for r in domain.rows]
        assert names == ["static", "dynamic", "static_r", "dynamic_r"]
        static_r = domain.rows[2]
        assert static_r.start == 1000
        assert static_r.length == 200
        assert static_r.end == 1200
        assert static_r.labels == 0
        assert static_r.last == 0

    def test_multiple_domains(self) -> None:
        text = (
            "1026-MPLS domain dom_a:\n"
            " Range         Start  Length     End  Labels    Last\n"
            " static           16     984    1000       0       0\n"
            "1026-\n"
            "1026-MPLS domain dom_b:\n"
            " Range         Start  Length     End  Labels    Last\n"
            " static         2000     500    2500       3    2042\n"
            "0000 \n"
        )
        result = parse_mpls_ranges(make_result_from_text(text))
        assert [d.domain for d in result.domains] == ["dom_a", "dom_b"]
        row = result.domains[1].rows[0]
        assert row.labels == 3
        assert row.last == 2042
