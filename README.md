# bird-api

A Python library for interacting with the [BIRD Internet Routing Daemon](https://github.com/CZ-NIC/bird) via its Unix control socket.

`bird-api` lets upper-layer software query a running BIRD instance, parse the textual
responses into structured Python objects, and at the same time retain the raw text
response for downstream display or logging.

## Features

- **Persistent connection** — one reusable Unix socket per `BirdClient`; transparent
  reconnect if the daemon restarts.
- **Structured + raw** — every result object carries typed fields *and* the untouched
  `raw_text` of the daemon's reply.
- **Complex queries, wrapped** — the full `show route` grammar through a fluent,
  validated `RouteQuery` builder; a typed `RouteFilter` algebra for `where` clauses;
  high-level helpers (`route_lookup`, `route_exists`, `count_routes`, `bgp_sessions`,
  `get_protocol`, protocol patterns like `"bgp*"`).
- **Protocol-specific queries** — static routes, the OSPF family (`show ospf`,
  neighbors, interface, state/topology, lsadb with filters), BFD sessions (table
  and verbose forms, with address/interface/family/mode filters), RIP and Babel
  interface/neighbor tables, and MPLS label ranges.
- **Asyncio support** — `AsyncBirdClient` mirrors the sync API with `async`
  signatures, shares the exact same validated command builders (byte-identical
  wire behaviour, enforced by parity tests) and serialises concurrent queries
  safely.
- **Injection-safe by construction** — every user-supplied value is validated before
  anything is written to the socket (see [Security](#security)).
- **Read-only mode** — `BirdClient(read_only=True)` blocks every mutating command
  client-side, before a single byte hits the wire.

## Requirements

- Python >= 3.13
- A running BIRD daemon with an accessible control socket (default `/run/bird/bird.ctl`
  on most systems; pass `socket_path=` to override).

## Installation

```bash
uv pip install -e .
```

## Quick start

```python
from bird_api import BirdClient

with BirdClient(socket_path="/run/bird/bird.ctl") as client:
    status = client.show_status()
    print(status.router_id)
    print(status.raw_text)          # the original BIRD reply, untouched

    for row in client.show_protocols(pattern="bgp*").rows:
        print(row.name, row.state, row.info)

    for entry in client.show_route_table("master4").entries:
        print(entry.prefix, entry.protocol, entry.preference)
```

## Complex queries

`RouteQuery` mirrors BIRD's full `show route` grammar (verified against the
`r_args` rule in `nest/config.Y`) as an immutable, validated builder:

```python
from bird_api import BirdClient, RouteFilter, RouteQuery

with BirdClient() as client:
    q = (
        RouteQuery()
        .for_address("203.0.113.7")          # best-route lookup for one IP
        .table("master4")                    # (repeatable) named tables
        # .table_all()                       # or: every table
        # .within("192.0.0.0/16")            # or: routes inside a prefix
        .where(RouteFilter.protocol("bgp1")
               & RouteFilter.prefix_len(min=8, max=24))
        .all_details()                       # attribute dump (show route all)
        .primary()                           # only best routes
    )
    result = client.show_route_query(q)
    for entry in result.entries:
        print(entry.prefix, entry.preference, entry.attributes.get("bgp_next_hop"))
```

`RouteFilter` builds filter expressions from individually validated parts and
composes with `&` (and), `|` (or), `~` (not):

| Constructor | Filter expression |
|---|---|
| `RouteFilter.protocol("bgp1")` | `proto = "bgp1"` |
| `RouteFilter.prefix("10.0.0.0/8")` | `net = 10.0.0.0/8` |
| `RouteFilter.prefix_in([...], or_longer=True)` | `net ~ [10.0.0.0/8+, …]` |
| `RouteFilter.prefix_len(min=16, max=24)` | `(net.len >= 16 && net.len <= 24)` |
| `RouteFilter.community(65000, 42)` | `(65000, 42) ~ bgp_community` |
| `RouteFilter.large_community(65000, 1, 2)` | `(65000, 1, 2) ~ bgp_large_community` |
| `RouteFilter.raw("…")` | arbitrary single-line expression (charset-checked) |

High-level helpers built on top of the query machinery:

```python
client.route_lookup("198.51.100.7", table="master4")   # miss -> empty result, not an error
client.route_exists("192.0.2.0/24", table="master4")   # -> bool
client.count_routes(table="master4")                   # -> int (counters only, no route dump)
client.get_protocol("static_test")                     # -> ProtocolDetail | None
client.find_protocols("bgp*")                          # shell-like pattern matching
client.bgp_sessions(pattern="peer_*")                  # structured BGP session table
```

### Protocol-specific queries

```python
# OSPF: area summary, neighbor table, interface details, LSDB
info = client.show_ospf()
assert info.areas[0].backbone and info.areas[0].neighbors_count == 0
neigh = client.show_ospf_neighbors(interface="eth*")     # quoted patmatch filter
lsas = client.show_ospf_lsadb(lsa_type=1, self_originated=True)

# BFD: live session table (and verbose form)
for row in client.show_bfd_sessions().rows:
    print(row.address, row.state, row.interval, row.timeout)
detail = client.show_bfd_sessions(all=True).details[0]
print(detail.session_type, detail.local_params["Multiplier"])

# Static / RIP / Babel / MPLS
routes = client.show_static()                            # StaticRoute objects
rip = client.show_rip_interfaces()                       # per-interface rows
babel = client.show_babel_neighbors(interface="eth0")
mpls = client.show_mpls_ranges()                         # per-domain ranges
```

### BGP sessions

`bgp_sessions()` decodes the BGP-specific `show protocols all` blocks into
`BgpSession` objects (state, neighbor address/AS/ID, session type, hold/keepalive
timers, capabilities…), while still carrying the full `raw_text`:

```python
sessions = client.bgp_sessions()
for s in sessions.sessions:
    print(s.name, s.state, s.neighbor_address, s.neighbor_as, s.established)
```

## Security

BIRD's control protocol is line-oriented: **every `\n`-terminated line is executed as
an independent command**. A naive client that interpolates user input into a command
string can be trivially injected:

```python
client.show_route_prefix("10.0.0.0/8\ndown")   # would stop BGP AND shut the daemon down!
```

`bird-api` closes this class of attacks with a layered validation module
(`bird_api.validation`):

1. **Line safety** (hard boundary): no control characters may appear in any value —
   newline smuggling is impossible by construction.
2. **Lexical safety**: each value class is restricted to the exact charset BIRD's
   lexer accepts for it — identifiers `[A-Za-z_][A-Za-z0-9_]*`, canonical prefixes
   from `ipaddress` (host bits rejected, mirroring the daemon), quote-free patterns,
   enum-checked options.
3. **Structural safety**: mutual exclusions are enforced client-side, and assembled
   commands are capped at BIRD's 4094-byte input buffer.

Invalid input raises `BirdValidationError` (a `BirdError` **and** a `ValueError`)
*before any I/O happens* — nothing is sent, the daemon is never touched.

For monitoring jobs that should never mutate state, use a read-only client:

```python
with BirdClient(read_only=True) as client:
    client.show_protocols()          # fine
    client.disable("bgp*")           # BirdValidationError — never reaches the socket
    client.raw("configure /x.conf")  # BirdValidationError — the escape hatch is gated too
```

The allowlist (`show …`, `configure status`, `restrict`) is enforced client-side, so
it survives transparent reconnects — unlike the daemon-side `restrict` command, which
is per-session. Combine both for defence in depth.

Free-form surfaces (`raw()`, `eval()`, `RouteFilter.raw()`) still accept the full
command/filter language, but only on a single printable-ASCII line: they cannot break
framing, so the worst case is a daemon-side syntax error, never a second command.

## Async usage

`AsyncBirdClient` has the same methods, validation and result models as
`BirdClient` — just `await` them:

```python
import asyncio
from bird_api import AsyncBirdClient, RouteQuery

async def main():
    async with AsyncBirdClient(socket_path="/tmp/bird.ctl") as client:
        status = await client.show_status()
        count = await client.count_routes(table="master4")
        q = RouteQuery().table("master4").protocol("bgp1").all_details()
        for entry in (await client.show_route_query(q)).entries:
            print(entry.prefix, entry.attributes.get("bgp_next_hop"))

    # Concurrent queries are fine — the connection serialises them safely:
    async with AsyncBirdClient() as client:
        bfd, ospf, rip = await asyncio.gather(
            client.show_bfd_sessions(),
            client.show_ospf(),
            client.show_rip_interfaces(),
        )

asyncio.run(main())
```

Both stacks build commands through the same `bird_api.commands` module, so
their wire behaviour is identical by construction (and enforced by tests
that replay a full call battery through both). The `on_async` callback
accepts plain functions or coroutine functions.

## Performance

- One persistent socket per client; no per-command connect/disconnect.
- Non-blocking I/O multiplexed with `select()`, `bytearray` receive buffer sliced
  on `\n` boundaries without rescans.
- Validation is a handful of precompiled-regex checks (microseconds per call).
- `count_routes()` uses `show route count` so counting a table with millions of
  routes transfers counters only, not route lines.

## Testing

```bash
# Unit tests (parsers, protocol, validation, query builders — no daemon needed)
.venv/bin/python -m pytest tests --ignore=tests/e2e

# End-to-end tests against live BIRD 3.1.7 (needs the helper daemons)
/home/z/my-project/scripts/start-bird.sh        # base daemon: /tmp/bird-test.ctl
/home/z/my-project/scripts/start-bgp-pair.sh    # protocol pair: /tmp/bird-bgp-a.ctl
.venv/bin/python -m pytest tests/e2e
```

The pair daemons run iBGP (established), a real multihop BFD session
(`127.0.0.2 <-> 127.0.0.3`, Up), OSPF/BFD/RIP/Babel protocol instances and
static routes. Sandbox limits: no `CAP_NET_RAW` means OSPF cannot form
adjacencies, and shared interfaces mean RIP/Babel have no peers — their
empty tables are asserted as such and row parsing is covered by
format-faithful synthetic fixtures. The BFD daemon pair needs an
`LD_PRELOAD` shim (`scripts/libpriorityshim.so`) because BIRD hardcodes
`SO_PRIORITY=7`, which unprivileged containers reject.

The e2e suite includes an injection-attack battery (framing and lexical payloads
against every input slot), read-only enforcement, complex-query round-trips and a
real iBGP session established over loopback between two BIRD instances — no root
required (port 1179 + `strict bind` on distinct 127/8 addresses).

Quality gates: `ruff check`, `mypy --strict` (0 errors), 521 tests.

## License

MIT
