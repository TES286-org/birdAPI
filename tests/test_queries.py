"""Tests for :mod:`bird_api.queries` — RouteQuery / RouteFilter builders.

Every test asserts both directions: valid inputs produce the exact wire
command, and injection payloads are rejected before a command can be built.
"""

from __future__ import annotations

import pytest

from bird_api.exceptions import BirdValidationError
from bird_api.queries import RouteFilter, RouteQuery


class TestRouteFilterLeaves:
    def test_protocol(self) -> None:
        assert RouteFilter.protocol("bgp1").expression == 'proto = "bgp1"'

    def test_protocol_injection_rejected(self) -> None:
        for payload in ["bgp1\ndown", "bgp 1", 'bgp"; down', "bgp*"]:
            with pytest.raises(BirdValidationError):
                RouteFilter.protocol(payload)

    def test_prefix_exact(self) -> None:
        assert RouteFilter.prefix("192.0.2.0/24").expression == "net = 192.0.2.0/24"

    def test_prefix_in(self) -> None:
        expr = RouteFilter.prefix_in(["192.0.2.0/24", "10.0.0.0/8"]).expression
        assert expr == "net ~ [192.0.2.0/24, 10.0.0.0/8]"

    def test_prefix_in_or_longer(self) -> None:
        expr = RouteFilter.prefix_in("10.0.0.0/8", or_longer=True).expression
        assert expr == "net ~ [10.0.0.0/8+]"

    def test_prefix_in_injection_rejected(self) -> None:
        with pytest.raises(BirdValidationError):
            RouteFilter.prefix_in(["10.0.0.0/8", "192.0.2.0/24\ndown"])

    def test_prefix_len_min_only(self) -> None:
        assert RouteFilter.prefix_len(min=16).expression == "net.len >= 16"

    def test_prefix_len_range(self) -> None:
        assert RouteFilter.prefix_len(min=16, max=24).expression == "(net.len >= 16 && net.len <= 24)"

    def test_prefix_len_requires_argument(self) -> None:
        with pytest.raises(ValueError, match="min and/or max"):
            RouteFilter.prefix_len()

    def test_prefix_len_bounds(self) -> None:
        with pytest.raises(BirdValidationError):
            RouteFilter.prefix_len(min=200)

    def test_community(self) -> None:
        assert RouteFilter.community(65000, 42).expression == "(65000, 42) ~ bgp_community"

    def test_large_community(self) -> None:
        expr = RouteFilter.large_community(65000, 100, 200).expression
        assert expr == "(65000, 100, 200) ~ bgp_large_community"

    def test_community_bounds(self) -> None:
        with pytest.raises(BirdValidationError):
            RouteFilter.community(65000, 70000)  # value > 16 bits

    def test_raw_validated(self) -> None:
        assert RouteFilter.raw("net.len > 8").expression == "net.len > 8"
        with pytest.raises(BirdValidationError):
            RouteFilter.raw("net.len > 8\ndown")
        with pytest.raises(BirdValidationError):
            RouteFilter.raw("héllo")


class TestRouteFilterComposition:
    def test_and(self) -> None:
        f = RouteFilter.protocol("bgp1") & RouteFilter.prefix_len(min=16)
        assert f.expression == '(proto = "bgp1" && net.len >= 16)'

    def test_or(self) -> None:
        f = RouteFilter.prefix("10.0.0.0/8") | RouteFilter.prefix("192.0.2.0/24")
        assert f.expression == "(net = 10.0.0.0/8 || net = 192.0.2.0/24)"

    def test_not(self) -> None:
        f = ~RouteFilter.protocol("bgp1")
        assert f.expression == '!(proto = "bgp1")'

    def test_complex_nesting(self) -> None:
        f = (RouteFilter.protocol("bgp1") & RouteFilter.prefix_len(max=24)) | (
            RouteFilter.protocol("static_test") & ~RouteFilter.prefix_len(min=8)
        )
        # All leaf parts validated; composition only adds ()/&&/||/!.
        assert "&&" in f.expression and "||" in f.expression

    def test_equality_and_hash(self) -> None:
        assert RouteFilter.protocol("x") == RouteFilter.protocol("x")
        assert RouteFilter.protocol("x") != RouteFilter.protocol("y")
        assert hash(RouteFilter.protocol("x")) == hash(RouteFilter.protocol("x"))


class TestRouteQueryBuild:
    def test_empty(self) -> None:
        assert RouteQuery().build() == "show route"

    def test_prefix(self) -> None:
        assert RouteQuery().prefix("192.0.2.0/24").build() == "show route 192.0.2.0/24"

    def test_for_address(self) -> None:
        assert RouteQuery().for_address("192.0.2.1").build() == "show route for 192.0.2.1"

    def test_for_prefix(self) -> None:
        assert RouteQuery().for_prefix("0.0.0.0/0").build() == "show route for 0.0.0.0/0"

    def test_within(self) -> None:
        assert RouteQuery().within("10.0.0.0/8").build() == "show route in 10.0.0.0/8"

    def test_tables(self) -> None:
        q = RouteQuery().table("master4").table("master6")
        assert q.build() == "show route table master4 table master6"

    def test_table_all(self) -> None:
        assert RouteQuery().table_all().build() == "show route table all"

    def test_table_all_count(self) -> None:
        assert RouteQuery().table_all().count().build() == "show route table all count"

    def test_import_export_table(self) -> None:
        assert RouteQuery().import_table("bgp1.ipv4").build() == "show route import table bgp1.ipv4"
        assert RouteQuery().export_table("bgp1").build() == "show route export table bgp1"

    def test_where(self) -> None:
        q = RouteQuery().where(RouteFilter.protocol("static_test"))
        assert q.build() == 'show route where proto = "static_test"'

    def test_where_string_coerced(self) -> None:
        q = RouteQuery().where("net.len > 8")
        assert q.build() == "show route where net.len > 8"
        with pytest.raises(BirdValidationError):
            RouteQuery().where("net.len > 8\ndown")

    def test_filter_name(self) -> None:
        assert RouteQuery().filter_name("my_filter").build() == "show route filter my_filter"

    def test_flags_and_protocol(self) -> None:
        q = RouteQuery().protocol("bgp1").all_details().primary().include_filtered()
        assert q.build() == "show route all primary filtered protocol bgp1"

    def test_export_modes(self) -> None:
        assert RouteQuery().export("bgp1").build() == "show route export bgp1"
        assert RouteQuery().preexport("bgp1.ipv4").build() == "show route preexport bgp1.ipv4"
        assert RouteQuery().noexport("bgp1").build() == "show route noexport bgp1"

    def test_stats_count(self) -> None:
        assert RouteQuery().stats().build() == "show route stats"
        assert RouteQuery().count().build() == "show route count"

    def test_kitchen_sink(self) -> None:
        q = (
            RouteQuery()
            .for_address("192.0.2.1")
            .table("master4")
            .where(RouteFilter.protocol("bgp1") & RouteFilter.prefix_len(min=8))
            .all_details()
            .primary()
        )
        assert q.build() == (
            "show route for 192.0.2.1 table master4 "
            'where (proto = "bgp1" && net.len >= 8) all primary'
        )


class TestRouteQueryMutualExclusions:
    def test_two_address_selectors(self) -> None:
        with pytest.raises(ValueError, match="Only one prefix"):
            RouteQuery().prefix("10.0.0.0/8").prefix("192.0.2.0/24")

    def test_two_address_selectors_for_and_within(self) -> None:
        with pytest.raises(ValueError, match="Only one prefix"):
            RouteQuery().for_address("10.0.0.1").within("10.0.0.0/8")

    def test_table_and_table_all(self) -> None:
        with pytest.raises(ValueError, match="table_all"):
            RouteQuery().table("master4").table_all().build()

    def test_table_and_import_table(self) -> None:
        with pytest.raises(ValueError, match="import/export table"):
            RouteQuery().table("master4").import_table("bgp1.ipv4").build()

    def test_import_and_export_table(self) -> None:
        with pytest.raises(ValueError, match="import_table"):
            RouteQuery().import_table("bgp1").export_table("bgp1").build()

    def test_filter_and_where(self) -> None:
        with pytest.raises(ValueError, match="Filter specified twice"):
            RouteQuery().filter_name("f").where(RouteFilter.raw("1 = 1")).build()

    def test_stats_and_count(self) -> None:
        with pytest.raises(ValueError, match="stats"):
            RouteQuery().stats().count().build()


class TestRouteQueryInjection:
    @pytest.mark.parametrize(
        "payload",
        ["bgp1\ndown", "bgp 1", "master4 extra", "bgp*;down", 'bgp";down', "tab\tchar"],
    )
    def test_rejected_in_typed_slots(self, payload: str) -> None:
        # Slots that must reject everything outside their strict charset.
        for build in (
            lambda: RouteQuery().prefix(payload),
            lambda: RouteQuery().for_address(payload),
            lambda: RouteQuery().within(payload),
            lambda: RouteQuery().table(payload),
            lambda: RouteQuery().protocol(payload),
            lambda: RouteQuery().filter_name(payload),
            lambda: RouteQuery().export(payload),
            lambda: RouteQuery().import_table(payload),
        ):
            with pytest.raises((BirdValidationError, ValueError)):
                build()

    @pytest.mark.parametrize(
        "payload",
        ["anything\ndown", "x\rdown", "a\x00b", "x\x7fy"],
    )
    def test_rejected_in_where_slot(self, payload: str) -> None:
        # The where() slot accepts the full filter language on one line,
        # so only control characters (framing safety) are rejected.
        with pytest.raises((BirdValidationError, ValueError)):
            RouteQuery().where(payload)

    def test_immutability(self) -> None:
        base = RouteQuery().table("master4")
        derived = base.protocol("bgp1")
        assert base.build() == "show route table master4"
        assert derived.build() == "show route table master4 protocol bgp1"
        assert base is not derived
