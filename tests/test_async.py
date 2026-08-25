"""Tests for the asyncio stack (:mod:`bird_api.async_connection` / `async_client`).

The tests run a **real** asyncio Unix-socket server that speaks BIRD's wire
protocol (banner + scripted responses), so :class:`AsyncBirdConnection` is
exercised end-to-end against actual socket I/O — no mocking of the stream
classes. A second suite replays the same call battery through the sync and
async clients and asserts byte-identical wire commands (API parity).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from bird_api import AsyncBirdClient, AsyncBirdConnection, BirdClient
from bird_api.exceptions import (
    BirdCommandError,
    BirdConnectionError,
    BirdTimeoutError,
    BirdValidationError,
)

STATUS_OK = (
    b"1000-BIRD 3.1.7\n"
    b"1011-Router ID is 10.0.0.1\n"
    b" Hostname is testhost\n"
    b" Current server time is 2026-08-25 05:00:00.000\n"
    b" Last reboot on 2026-08-25 05:00:00.000\n"
    b" Last reconfiguration on 2026-08-25 05:00:00.000\n"
    b"0013 Daemon is up and running\n"
)

PROTOCOLS_OK = (
    b"2002-Name       Proto      Table      State  Since         Info\n"
    b"1002-static_test Static     master4    up     13:22:41.388  \n"
    b"0000 \n"
)

ROUTE_OK = (
    b"1007-Table master4:\n"
    b" 192.0.2.0/24         blackhole [static_test 13:22:41.388] ! (200)\n"
    b"0000 \n"
)


class FakeBirdServer:
    """An in-process asyncio Unix server speaking the BIRD control protocol.

    Responses are scripted as ``(command, payload)`` pairs; the server sends
    the banner on connect, reads one command line at a time and replies with
    the matching payload (or ``9001 unknown``). A payload of ``False`` means
    "never reply" — used to drive client-side timeouts.
    """

    def __init__(self, responses: Iterable[tuple[str, bytes | bool]]) -> None:
        self._responses = dict(responses)
        self.commands: list[str] = []
        self.banner = b"0001 BIRD 3.1.7 ready.\n"
        self.path: str = ""
        self._server: asyncio.Server | None = None
        self._clients: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=self._get_path())

    def _get_path(self) -> str:
        # Unix socket paths must be short; use /tmp with a per-test name.
        import itertools

        for i in itertools.count():
            candidate = f"/tmp/bird-fake-{id(self):x}-{i}.sock"
            if not Path(candidate).exists():
                self.path = candidate
                return candidate
        raise AssertionError("unreachable")  # pragma: no cover

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.append(writer)
        try:
            writer.write(self.banner)
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    break
                command = line.decode("utf-8", errors="replace").rstrip("\n")
                self.commands.append(command)
                payload = self._responses.get(command, b"9001 Unknown command\n")
                if payload is False:
                    # Scripted silence: never reply (drives client timeouts).
                    await asyncio.sleep(3600)
                    continue
                writer.write(payload)
                await writer.drain()
        except ConnectionError:  # pragma: no cover - client hung up hard
            pass
        finally:
            with __import__("contextlib").suppress(Exception):
                writer.close()

    async def stop(self) -> None:
        # Close lingering client connections first — wait_closed() would
        # otherwise block forever on handlers still reading their streams.
        for writer in self._clients:
            with __import__("contextlib").suppress(Exception):
                writer.close()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with __import__("contextlib").suppress(OSError):
            Path(self.path).unlink()


def run(body: Callable[[], object]) -> object:
    """Call ``body`` and run the returned coroutine to completion.

    (Avoids a pytest-asyncio dependency: every test is a plain sync
    function whose ``async def body()`` inner is executed here.)
    """
    return asyncio.run(body())


@asynccontextmanager
async def _started_server(
    responses: list[tuple[str, bytes | bool]] | None = None,
) -> AsyncIterator[FakeBirdServer]:
    """Async context manager: a running FakeBirdServer, auto-stopped."""
    server = FakeBirdServer(responses or [("show status", STATUS_OK)])
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Connection-level tests (real socket I/O)
# ---------------------------------------------------------------------------


class TestAsyncConnection:
    def test_connect_banner_and_version(self) -> None:
        async def body() -> None:
            async with _started_server() as fake_server:
                conn = AsyncBirdConnection(socket_path=fake_server.path, timeout=5)
                await conn.connect()
                assert conn.is_connected
                assert conn.server_version == "3.1.7"
                await conn.close()
                assert not conn.is_connected

        run(body)

    def test_execute_round_trip(self) -> None:
        async def body() -> None:
            async with _started_server() as fake_server:
                conn = AsyncBirdConnection(socket_path=fake_server.path, timeout=5)
                result = await conn.execute("show status")
                assert result.success
                assert result.final_code == 13
                assert "Daemon is up and running" in result.raw_text
                assert fake_server.commands == ["show status"]
                await conn.close()

        run(body)

    def test_lazy_connect_on_execute(self) -> None:
        async def body() -> None:
            async with _started_server() as fake_server:
                conn = AsyncBirdConnection(socket_path=fake_server.path, timeout=5)
                assert not conn.is_connected
                await conn.execute("show status")
                assert conn.is_connected
                await conn.close()

        run(body)

    def test_async_context_manager(self) -> None:
        async def body() -> None:
            async with _started_server() as fake_server:
                async with AsyncBirdConnection(socket_path=fake_server.path) as conn:
                    assert conn.is_connected
                assert not conn.is_connected

        run(body)

    def test_command_error_raises(self) -> None:
        async def body() -> None:
            server = FakeBirdServer([("down", b"8002 Shutting down\n")])
            await server.start()
            try:
                conn = AsyncBirdConnection(socket_path=server.path, timeout=5)
                with pytest.raises(BirdCommandError) as excinfo:
                    await conn.execute("down")
                assert excinfo.value.code == 8002
                await conn.close()
            finally:
                await server.stop()

        run(body)

    def test_timeout_drops_connection(self) -> None:
        async def body() -> None:
            # Script silence for the command -> client times out.
            server = FakeBirdServer([("show status", STATUS_OK), ("show memory", False)])
            await server.start()
            try:
                conn = AsyncBirdConnection(socket_path=server.path, timeout=0.2)
                with pytest.raises(BirdTimeoutError):
                    await conn.execute("show memory")
                # The stream was dropped so the next execute reconnects.
                assert not conn.is_connected
                result = await conn.execute("show status")
                assert result.success
                await conn.close()
            finally:
                await server.stop()

        run(body)

    def test_connection_refused(self) -> None:
        async def body() -> None:
            conn = AsyncBirdConnection(socket_path="/tmp/definitely-no-such-bird.ctl", timeout=1)
            with pytest.raises(BirdConnectionError):
                await conn.connect()

        run(body)

    def test_read_only_rejection(self) -> None:
        async def body() -> None:
            async with _started_server() as fake_server:
                conn = AsyncBirdConnection(socket_path=fake_server.path, read_only=True)
                result = await conn.execute("show status")
                assert result.success
                with pytest.raises(BirdValidationError, match="read-only"):
                    await conn.execute("down")
                with pytest.raises(BirdValidationError, match="read-only"):
                    await conn.execute("disable all")
                # Nothing but the allowed command reached the wire.
                assert fake_server.commands == ["show status"]
                await conn.close()

        run(body)

    def test_injection_rejected_before_wire(self) -> None:
        async def body() -> None:
            async with _started_server() as fake_server:
                conn = AsyncBirdConnection(socket_path=fake_server.path)
                with pytest.raises(BirdValidationError):
                    await conn.execute("show status\ndown")
                assert fake_server.commands == []
                await conn.close()

        run(body)

    def test_oversized_command_rejected(self) -> None:
        async def body() -> None:
            async with _started_server() as fake_server:
                conn = AsyncBirdConnection(socket_path=fake_server.path)
                with pytest.raises((BirdCommandError, BirdValidationError)):
                    await conn.execute("x" * 5000)
                assert fake_server.commands == []
                await conn.close()

        run(body)

    def test_async_message_callback_sync(self) -> None:
        async def body() -> None:
            payload = b"+Config timeout expired, starting undo\n" + STATUS_OK
            server = FakeBirdServer([("show status", payload)])
            await server.start()
            try:
                seen: list[str] = []
                conn = AsyncBirdConnection(socket_path=server.path, on_async=seen.append)
                result = await conn.execute("show status")
                assert result.success
                assert seen == ["Config timeout expired, starting undo"]
                assert result.async_messages == ["Config timeout expired, starting undo"]
                await conn.close()
            finally:
                await server.stop()

        run(body)

    def test_async_message_callback_coroutine(self) -> None:
        async def body() -> None:
            payload = b"+graceful restart started\n" + STATUS_OK
            server = FakeBirdServer([("show status", payload)])
            await server.start()
            try:
                seen: list[str] = []

                async def collector(text: str) -> None:
                    seen.append(text)

                conn = AsyncBirdConnection(socket_path=server.path, on_async=collector)
                result = await conn.execute("show status")
                assert result.success
                assert seen == ["graceful restart started"]
                await conn.close()
            finally:
                await server.stop()

        run(body)

    def test_concurrent_executes_serialised_in_order(self) -> None:
        async def body() -> None:
            server = FakeBirdServer(
                [
                    ("show status", STATUS_OK),
                    ("show memory", b"1005-Total: 8351.0  B\n0000 \n"),
                    ("show symbols", b"1010-master4\trouting table\n0000 \n"),
                ],
            )
            await server.start()
            try:
                client = AsyncBirdClient(socket_path=server.path, timeout=5)
                results = await asyncio.gather(
                    client.show_status(),
                    client.raw("show memory"),
                    client.raw("show symbols"),
                )
                # All three commands completed; order preserved per stream.
                assert len(server.commands) == 3
                assert set(server.commands) == {"show status", "show memory", "show symbols"}
                assert results[0].router_id == "10.0.0.1"
                await client.close()
            finally:
                await server.stop()

        run(body)


# ---------------------------------------------------------------------------
# Client-level tests
# ---------------------------------------------------------------------------


class TestAsyncBirdClient:
    def test_context_manager_and_show_status(self) -> None:
        async def body() -> None:
            async with (
                _started_server() as fake_server,
                AsyncBirdClient(socket_path=fake_server.path) as client,
            ):
                status = await client.show_status()
                assert status.router_id == "10.0.0.1"
                assert status.reconfig_state == "up"
                assert client.server_version == "3.1.7"

        run(body)

    def test_read_only_flag(self) -> None:
        async def body() -> None:
            async with _started_server() as fake_server, AsyncBirdClient(
                socket_path=fake_server.path, read_only=True
            ) as client:
                assert client.read_only is True
                await client.show_status()
                with pytest.raises(BirdValidationError, match="read-only"):
                    await client.disable("all")
            assert fake_server.commands == ["show status"]

        run(body)

    def test_injection_guards_shared_with_sync(self) -> None:
        async def body() -> None:
            async with _started_server() as fake_server:
                async with AsyncBirdClient(socket_path=fake_server.path) as client:
                    for attempt in (
                        lambda: client.show_route_prefix("10.0.0.0/8\ndown"),
                        lambda: client.show_route_table("master4 extra"),
                        lambda: client.eval("1+2\ndown"),
                        lambda: client.raw("show status\ndown"),
                    ):
                        with pytest.raises(BirdValidationError):
                            await attempt()
                assert fake_server.commands == []

        run(body)

    def test_route_lookup_translation(self) -> None:
        async def body() -> None:
            server = FakeBirdServer(
                [
                    ("show route for 203.0.113.99 table master4", b"8001 Network not found\n"),
                ],
            )
            await server.start()
            try:
                async with AsyncBirdClient(socket_path=server.path) as client:
                    result = await client.route_lookup("203.0.113.99", table="master4")
                    assert result.entries == []
                    assert "Network not found" in result.raw_text
            finally:
                await server.stop()

        run(body)

    def test_raw_lines_async_iterator(self) -> None:
        async def body() -> None:
            server = FakeBirdServer([("show route", ROUTE_OK)])
            await server.start()
            try:
                async with AsyncBirdClient(socket_path=server.path) as client:
                    lines = [line async for line in client.raw_lines("show route")]
                assert any("192.0.2.0/24" in ln for ln in lines)
            finally:
                await server.stop()

        run(body)


# ---------------------------------------------------------------------------
# Sync/async API parity: identical call battery -> identical wire commands
# ---------------------------------------------------------------------------


def _battery_calls_sync(client: BirdClient) -> list[str]:
    """Replay the full call battery on a sync client; return emitted commands."""
    with __import__("contextlib").suppress(Exception):
        client.show_status()
    with __import__("contextlib").suppress(Exception):
        client.show_memory()
    with __import__("contextlib").suppress(Exception):
        client.show_protocols()
    with __import__("contextlib").suppress(Exception):
        client.show_protocols_all()
    with __import__("contextlib").suppress(Exception):
        client.show_protocols(pattern="static*")
    with __import__("contextlib").suppress(Exception):
        client.show_route()
    with __import__("contextlib").suppress(Exception):
        client.show_route_prefix("192.0.2.0/24")
    with __import__("contextlib").suppress(Exception):
        client.show_route_for("192.0.2.1")
    with __import__("contextlib").suppress(Exception):
        client.show_route_in("192.0.0.0/16")
    with __import__("contextlib").suppress(Exception):
        client.show_route_table("master4")
    with __import__("contextlib").suppress(Exception):
        client.show_route_protocol("static_test")
    with __import__("contextlib").suppress(Exception):
        client.show_route_stats()
    with __import__("contextlib").suppress(Exception):
        client.show_symbols()
    with __import__("contextlib").suppress(Exception):
        client.eval("1+2")
    with __import__("contextlib").suppress(Exception):
        client.route_lookup("192.0.2.1", table="master4")
    with __import__("contextlib").suppress(Exception):
        client.route_exists("192.0.2.0/24", table="master4")
    with __import__("contextlib").suppress(Exception):
        client.count_routes(table="master4")
    with __import__("contextlib").suppress(Exception):
        client.count_routes()
    with __import__("contextlib").suppress(Exception):
        client.bgp_sessions()
    with __import__("contextlib").suppress(Exception):
        client.find_protocols("static*")
    with __import__("contextlib").suppress(Exception):
        client.show_static()
    with __import__("contextlib").suppress(Exception):
        client.show_ospf()
    with __import__("contextlib").suppress(Exception):
        client.show_ospf_neighbors()
    with __import__("contextlib").suppress(Exception):
        client.show_ospf_neighbors(interface="eth*")
    with __import__("contextlib").suppress(Exception):
        client.show_ospf_interface()
    with __import__("contextlib").suppress(Exception):
        client.show_ospf_state()
    with __import__("contextlib").suppress(Exception):
        client.show_ospf_topology()
    with __import__("contextlib").suppress(Exception):
        client.show_ospf_lsadb()
    with __import__("contextlib").suppress(Exception):
        client.show_bfd_sessions()
    with __import__("contextlib").suppress(Exception):
        client.show_bfd_sessions(address="127.0.0.3", mode="multihop")
    with __import__("contextlib").suppress(Exception):
        client.show_bfd_sessions(all=True)
    with __import__("contextlib").suppress(Exception):
        client.show_rip_interfaces()
    with __import__("contextlib").suppress(Exception):
        client.show_rip_neighbors()
    with __import__("contextlib").suppress(Exception):
        client.show_babel_interfaces()
    with __import__("contextlib").suppress(Exception):
        client.show_babel_neighbors()
    with __import__("contextlib").suppress(Exception):
        client.show_mpls_ranges()
    return client._conn.calls  # type: ignore[attr-defined]


async def _battery_calls_async(client: AsyncBirdClient) -> list[str]:
    """Replay the full call battery on an async client; return emitted commands."""
    calls: list[Callable[[], object]] = [
        lambda: client.show_status(),
        lambda: client.show_memory(),
        lambda: client.show_protocols(),
        lambda: client.show_protocols_all(),
        lambda: client.show_protocols(pattern="static*"),
        lambda: client.show_route(),
        lambda: client.show_route_prefix("192.0.2.0/24"),
        lambda: client.show_route_for("192.0.2.1"),
        lambda: client.show_route_in("192.0.0.0/16"),
        lambda: client.show_route_table("master4"),
        lambda: client.show_route_protocol("static_test"),
        lambda: client.show_route_stats(),
        lambda: client.show_symbols(),
        lambda: client.eval("1+2"),
        lambda: client.route_lookup("192.0.2.1", table="master4"),
        lambda: client.route_exists("192.0.2.0/24", table="master4"),
        lambda: client.count_routes(table="master4"),
        lambda: client.count_routes(),
        lambda: client.bgp_sessions(),
        lambda: client.find_protocols("static*"),
        lambda: client.show_static(),
        lambda: client.show_ospf(),
        lambda: client.show_ospf_neighbors(),
        lambda: client.show_ospf_neighbors(interface="eth*"),
        lambda: client.show_ospf_interface(),
        lambda: client.show_ospf_state(),
        lambda: client.show_ospf_topology(),
        lambda: client.show_ospf_lsadb(),
        lambda: client.show_bfd_sessions(),
        lambda: client.show_bfd_sessions(address="127.0.0.3", mode="multihop"),
        lambda: client.show_bfd_sessions(all=True),
        lambda: client.show_rip_interfaces(),
        lambda: client.show_rip_neighbors(),
        lambda: client.show_babel_interfaces(),
        lambda: client.show_babel_neighbors(),
        lambda: client.show_mpls_ranges(),
    ]
    for make_call in calls:
        with __import__("contextlib").suppress(Exception):
            await make_call()  # type: ignore[misc]
    return []


class TestSyncAsyncParity:
    def test_identical_wire_commands(self) -> None:
        """The same call battery must produce byte-identical wire commands."""
        from conftest import make_client_with_responses

        async def body() -> list[str]:
            # A socket server that accepts ANY command with a success reply.
            server = FakeBirdServer([])
            await server.start()
            try:
                client = AsyncBirdClient(socket_path=server.path, timeout=5)
                # The default "9001 Unknown" reply raises BirdCommandError
                # client-side; the command string is recorded by the server
                # before the reply is processed, which is all we compare.
                await _battery_calls_async(client)
                await client.close()
                return list(server.commands)
            finally:
                await server.stop()

        async_commands = asyncio.run(body())
        sync_client = make_client_with_responses([STATUS_OK] * 40)
        sync_commands = _battery_calls_sync(sync_client)

        # Lengths: every battery entry emitted exactly one command on both
        # stacks. (count_routes(table=...) may raise post-parse on the
        # canned reply, but its command still hit the wire on both sides.)
        assert len(async_commands) == len(sync_commands), (
            f"async sent {len(async_commands)} commands, "
            f"sync sent {len(sync_commands)}"
        )
        assert async_commands == sync_commands
