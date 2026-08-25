"""End-to-end tests for the protocol-specific query commands and the async stack.

Targets the pair daemons started by ``scripts/start-bgp-pair.sh``
(``/tmp/bird-bgp-a.ctl`` — BGP, OSPF, BFD, RIP, Babel, static) and the base
daemon (``/tmp/bird-test.ctl`` — static routes, MPLS domain). The modules
are skipped when the corresponding socket is unreachable.

Sandbox limitations (documented in ``docs/bird-protocol-research.md`` §7.5):
no raw sockets (CAP_NET_RAW) means OSPF cannot form adjacencies, and shared
interfaces mean RIP/Babel cannot exchange peers — their neighbor tables are
empty and the parsers' row handling is covered by synthetic unit fixtures.
The BFD session between the two daemons is real and established.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from test_e2e import SOCKET_PATH, _socket_reachable

from bird_api import AsyncBirdClient, BirdClient, RouteQuery
from bird_api.exceptions import BirdCommandError, BirdValidationError

PAIR_SOCKET = os.environ.get("BIRD_PAIR_SOCKET", "/tmp/bird-bgp-a.ctl")

base_daemon_required = pytest.mark.skipif(
    not _socket_reachable(SOCKET_PATH),
    reason=f"no reachable BIRD control socket at {SOCKET_PATH!r}",
)
pair_daemon_required = pytest.mark.skipif(
    not _socket_reachable(PAIR_SOCKET),
    reason=f"no reachable pair daemon at {PAIR_SOCKET!r}",
)


@pytest.fixture(scope="module")
def client() -> BirdClient:
    c = BirdClient(socket_path=PAIR_SOCKET, timeout=10.0)
    c.connect()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------


@base_daemon_required
class TestStaticE2E:
    def test_static_routes(self) -> None:
        with BirdClient(socket_path=SOCKET_PATH, timeout=10.0) as c:
            result = c.show_static()
        assert len(result.routes) == 5
        by_prefix = {r.prefix: r for r in result.routes}
        assert by_prefix["192.0.2.0/24"].dest_type == "blackhole"
        assert by_prefix["10.0.0.0/8"].dest_type == "blackhole"

    def test_static_with_protocol_name(self) -> None:
        with BirdClient(socket_path=SOCKET_PATH, timeout=10.0) as c:
            result = c.show_static("static_test")
        assert {r.prefix for r in result.routes} == {
            "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
        }

    def test_static_unknown_protocol_is_command_error(self) -> None:
        with BirdClient(socket_path=SOCKET_PATH, timeout=10.0) as c, pytest.raises(BirdCommandError):
            c.show_static("no_such_static")


# ---------------------------------------------------------------------------
# OSPF (single instance; adjacency impossible without CAP_NET_RAW)
# ---------------------------------------------------------------------------


@pair_daemon_required
class TestOspfE2E:
    def test_show_ospf_summary(self, client: BirdClient) -> None:
        result = client.show_ospf()
        assert result.up
        assert result.protocol == "ospf_a"
        assert result.num_areas == 1
        assert result.areas[0].area_id == "0.0.0.0"
        assert result.areas[0].backbone
        assert result.areas[0].interfaces_count == 1

    def test_show_ospf_with_name(self, client: BirdClient) -> None:
        result = client.show_ospf("ospf_a")
        assert result.protocol == "ospf_a"

    def test_show_ospf_neighbors_empty(self, client: BirdClient) -> None:
        result = client.show_ospf_neighbors()
        assert result.up
        assert result.rows == []
        assert result.protocol == "ospf_a"

    def test_show_ospf_interface(self, client: BirdClient) -> None:
        result = client.show_ospf_interface()
        assert len(result.interfaces) == 1
        iface = result.interfaces[0]
        assert iface.name == "eth0"
        assert iface.type == "ptp"
        assert "stub" in (iface.state or "")
        assert iface.cost is not None and iface.cost > 0

    def test_show_ospf_interface_with_filter(self, client: BirdClient) -> None:
        result = client.show_ospf_interface("ospf_a", "eth*")
        assert len(result.interfaces) == 1
        # A non-matching pattern yields no interface blocks (still up).
        empty = client.show_ospf_interface("ospf_a", "nosuch*")
        assert empty.interfaces == []

    def test_show_ospf_state(self, client: BirdClient) -> None:
        result = client.show_ospf_state()
        assert result.up
        assert result.areas == ["area 0.0.0.0"]
        assert "router 10.0.0.1" in result.routers
        # The router's own stub network appears in the topology dump.
        assert any("stubnet" in ln for ln in result.lines)

    def test_show_ospf_topology(self, client: BirdClient) -> None:
        result = client.show_ospf_topology()
        assert result.areas == ["area 0.0.0.0"]

    def test_show_ospf_lsadb(self, client: BirdClient) -> None:
        result = client.show_ospf_lsadb()
        assert result.up
        # At least the self-originated router LSA in area 0.
        assert any(row.type == "0001" for row in result.rows)
        assert all(row.router for row in result.rows)

    def test_show_ospf_lsadb_filters(self, client: BirdClient) -> None:
        everything = client.show_ospf_lsadb()
        typed = client.show_ospf_lsadb(lsa_type=1)
        assert len(typed.rows) >= 1
        assert all(int(row.type, 16) & 0xFF == 1 for row in typed.rows)
        self_originated = client.show_ospf_lsadb(self_originated=True)
        assert len(self_originated.rows) == len(everything.rows)
        global_scope = client.show_ospf_lsadb(scope="global")
        assert global_scope.rows == []


# ---------------------------------------------------------------------------
# BFD (real session between the pair daemons)
# ---------------------------------------------------------------------------


@pair_daemon_required
class TestBfdE2E:
    def test_sessions_table(self, client: BirdClient) -> None:
        result = client.show_bfd_sessions()
        assert result.up
        assert result.protocols == ["bfd_test"]
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.address == "127.0.0.3"
        assert row.state == "Up"
        assert row.interface == "---"  # multihop session

    def test_sessions_verbose(self, client: BirdClient) -> None:
        result = client.show_bfd_sessions(all=True)
        assert result.verbose
        detail = result.details[0]
        assert detail.address == "127.0.0.3"
        assert detail.state == "Up"
        assert detail.session_type == "Multihop"
        assert detail.fields["Local discriminator"].isdigit()
        assert detail.local_params["Multiplier"].isdigit()

    def test_sessions_address_filter(self, client: BirdClient) -> None:
        result = client.show_bfd_sessions(address="127.0.0.3")
        assert len(result.rows) == 1
        missed = client.show_bfd_sessions(address="192.0.2.99")
        assert missed.rows == []

    def test_sessions_mode_filter(self, client: BirdClient) -> None:
        direct = client.show_bfd_sessions(mode="direct")
        assert direct.rows == []
        multihop = client.show_bfd_sessions(mode="multihop")
        assert len(multihop.rows) == 1

    def test_sessions_interface_filter(self, client: BirdClient) -> None:
        result = client.show_bfd_sessions(interface="eth0")
        assert result.rows == []  # the only session is multihop ("---")


# ---------------------------------------------------------------------------
# RIP / Babel (interface tables; no peers possible in the sandbox)
# ---------------------------------------------------------------------------


@pair_daemon_required
class TestRipBabelE2E:
    def test_rip_interfaces(self, client: BirdClient) -> None:
        result = client.show_rip_interfaces()
        assert result.up
        assert result.protocol == "rip_test"
        assert len(result.rows) == 1
        assert result.rows[0].interface == "eth0"
        assert result.rows[0].state == "Up"
        assert result.rows[0].metric == 1

    def test_rip_neighbors_empty(self, client: BirdClient) -> None:
        result = client.show_rip_neighbors()
        assert result.up
        assert result.rows == []

    def test_babel_interfaces(self, client: BirdClient) -> None:
        result = client.show_babel_interfaces()
        assert result.up
        assert result.protocol == "babel_test"
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.interface == "eth0"
        assert row.state == "Up"
        assert row.rxcost == 96

    def test_babel_neighbors_empty(self, client: BirdClient) -> None:
        result = client.show_babel_neighbors()
        assert result.up
        assert result.rows == []

    def test_interface_filter_validation(self, client: BirdClient) -> None:
        with pytest.raises(BirdValidationError):
            client.show_rip_neighbors(interface="eth0\"\ndown")
        with pytest.raises(BirdValidationError):
            client.show_babel_interfaces("x\ndown")


# ---------------------------------------------------------------------------
# MPLS
# ---------------------------------------------------------------------------


@base_daemon_required
class TestMplsE2E:
    def test_ranges(self) -> None:
        with BirdClient(socket_path=SOCKET_PATH, timeout=10.0) as c:
            result = c.show_mpls_ranges()
        assert len(result.domains) == 1
        domain = result.domains[0]
        assert domain.domain == "test_dom"
        names = {r.name for r in domain.rows}
        assert {"static", "dynamic", "static_r", "dynamic_r"} <= names
        by_name = {r.name: r for r in domain.rows}
        assert by_name["static_r"].start == 1000
        assert by_name["dynamic_r"].length == 1000


# ---------------------------------------------------------------------------
# Async stack against the live daemons
# ---------------------------------------------------------------------------


@pair_daemon_required
class TestAsyncE2E:
    def test_full_async_session(self) -> None:
        async def body() -> dict[str, object]:
            out: dict[str, object] = {}
            async with AsyncBirdClient(socket_path=PAIR_SOCKET, timeout=10.0) as client:
                status = await client.show_status()
                out["version"] = status.bird_version
                out["router_id"] = status.router_id

                bfd = await client.show_bfd_sessions()
                out["bfd_state"] = bfd.rows[0].state if bfd.rows else "none"

                ospf = await client.show_ospf()
                out["ospf_areas"] = len(ospf.areas)

                count = await client.count_routes(table="master4")
                out["routes"] = count

                lookup = await client.route_lookup("192.0.2.1", table="master4")
                out["lookup"] = any(e.prefix == "192.0.2.0/24" for e in lookup.entries)

                q = RouteQuery().table("master4").prefix("192.0.2.0/24").all_details()
                detailed = await client.show_route_query(q)
                out["attrs"] = detailed.entries[0].attributes.get("source")

                bgp = await client.bgp_sessions()
                out["bgp_established"] = bgp.sessions[0].established if bgp.sessions else False
            return out

        result = asyncio.run(body())
        assert result["version"]
        assert result["router_id"] == "10.0.0.1"
        assert result["bfd_state"] == "Up"
        assert result["ospf_areas"] == 1
        # Pair daemon A: static 192.0.2.0/24 + BGP-learned 198.51.100.0/24
        # (+ any OSPF-injected stub routes on other BIRD versions).
        assert isinstance(result["routes"], int) and result["routes"] >= 2
        assert result["lookup"] is True
        assert result["attrs"] == "static"
        assert result["bgp_established"] is True

    def test_async_concurrent_queries(self) -> None:
        """Fire many queries concurrently — they must all succeed with
        correct, non-interleaved results (the connection serialises)."""

        async def body() -> list[object]:
            async with AsyncBirdClient(socket_path=PAIR_SOCKET, timeout=15.0) as client:
                results = await asyncio.gather(
                    client.show_ospf_neighbors(),
                    client.show_bfd_sessions(),
                    client.show_rip_interfaces(),
                    client.show_babel_interfaces(),
                    client.show_static(),
                    client.show_protocols(),
                )
            return list(results)

        results = asyncio.run(body())
        ospf_n, bfd, rip, babel, static, protocols = results
        assert ospf_n.rows == []  # no OSPF adjacency possible without CAP_NET_RAW
        assert len(bfd.rows) == 1 and bfd.rows[0].state == "Up"
        assert len(rip.rows) == 1 and rip.rows[0].interface == "eth0"
        assert len(babel.rows) == 1
        assert len(static.routes) == 1 and static.routes[0].prefix == "192.0.2.0/24"
        assert len(protocols.rows) >= 5

    def test_async_read_only_and_injection(self) -> None:
        async def body() -> None:
            async with AsyncBirdClient(
                socket_path=PAIR_SOCKET, read_only=True, timeout=10.0
            ) as client:
                await client.show_status()
                with pytest.raises(BirdValidationError, match="read-only"):
                    await client.disable("all")
                with pytest.raises(BirdValidationError):
                    await client.show_route_prefix("10.0.0.0/8\ndown")

        asyncio.run(body())

    def test_async_matches_sync_results(self) -> None:
        """Same queries through both stacks must yield equal structured data."""

        async def async_side() -> list[str]:
            async with AsyncBirdClient(socket_path=PAIR_SOCKET, timeout=10.0) as client:
                bfd = await client.show_bfd_sessions()
                lsadb = await client.show_ospf_lsadb()
                return [
                    f"bfd:{r.address}:{r.state}" for r in bfd.rows
                ] + [
                    f"lsa:{row.type}:{row.router}" for row in lsadb.rows
                ]

        sync_client = BirdClient(socket_path=PAIR_SOCKET, timeout=10.0)
        sync_client.connect()
        try:
            bfd = sync_client.show_bfd_sessions()
            lsadb = sync_client.show_ospf_lsadb()
            sync_side = [
                f"bfd:{r.address}:{r.state}" for r in bfd.rows
            ] + [
                f"lsa:{row.type}:{row.router}" for row in lsadb.rows
            ]
        finally:
            sync_client.close()

        assert asyncio.run(async_side()) == sync_side
