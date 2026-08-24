# bird-api

A Python library for interacting with the [BIRD Internet Routing Daemon](https://github.com/CZ-NIC/bird) via its Unix control socket.

`bird-api` lets upper-layer software query a running BIRD instance, parse the textual
responses into structured Python objects, and at the same time retain the raw text
response for downstream display or logging.

## Goals

- One persistent, reusable Unix socket connection per `BirdClient` instance.
- Structured, typed responses for the most common BIRD commands.
- Always-available raw text copy of every response.
- Easy to extend with new command parsers.

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

client = BirdClient(socket_path="/run/bird/bird.ctl")
client.connect()

status = client.show_status()
print(status.router_id)
print(status.raw_text)   # the original BIRD reply, untouched

for route in client.show_route().iter_entries():
    print(route.prefix, route.protocol)
```

## License

MIT
