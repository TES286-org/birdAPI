"""Parsers for the OSPF ``show`` command family.

Covers ``show ospf`` (1014), ``show ospf neighbors`` (1013),
``show ospf interface`` (1015), ``show ospf state``/``topology`` (1016) and
``show ospf lsadb`` (1017). All commands answer a single
``<name>: is not up`` line when the protocol is not running, reflected by
``up=False`` on the result.

Output formats (proto/ospf/ospf.c):

* ``show ospf`` — per-protocol summary with nested area blocks::

      1014-ospf_a:
       RFC1583 compatibility: disabled
       Stub router: No
       RT scheduler tick: 1
       Number of areas: 1
       Number of LSAs in DB:\t1
      \tArea: 0.0.0.0 (0) [BACKBONE]
      \t\tStub:\tNo
      \t\tNumber of interfaces:\t1

* ``show ospf neighbors`` — tab-separated table::

      1013-ospf_a:
       Router ID   \tPri\t     State     \tDTime\tInterface  Router IP
       10.0.0.2    \t  1\tFull/PtP     \t   32\teth0       21.0.22.68

* ``show ospf interface`` — per-interface key/value blocks::

      1015-ospf_a:
       Interface eth0 (21.0.22.68/32)
      \tType: ptp
      \tArea: 0.0.0.0 (0)

* ``show ospf state`` — indented topology tree (kept as lines + headers).

* ``show ospf lsadb`` — scoped LSA tables::

      1017-
       Area 0.0.0.0
       Type   LS ID           Router          Sequence   Age  Checksum
       0001  10.0.0.1        10.0.0.1         80000001    75    1f6e
"""

from __future__ import annotations

import re

from ..models import (
    OspfArea,
    OspfInfo,
    OspfInterface,
    OspfInterfacesResult,
    OspfLsadbResult,
    OspfLsadbScope,
    OspfLsaRow,
    OspfNeighborRow,
    OspfNeighborsResult,
    OspfStateResult,
)
from ..protocol import CommandResult

__all__ = [
    "parse_ospf",
    "parse_ospf_neighbors",
    "parse_ospf_interfaces",
    "parse_ospf_state",
    "parse_ospf_lsadb",
]

_NOT_UP_RE = re.compile(r"^(?P<name>\S+):\s+is not up\s*$")
_PROTOCOL_HEADER_RE = re.compile(r"^(?P<name>\S+):\s*$")

_AREA_HEADER_RE = re.compile(r"^Area:\s+(?P<id>\S+)\s+\((?P<num>\d+)\)(?:\s+(?P<extra>.*))?$")
_AREA_FIELD_RE = re.compile(r"^(?P<key>Number of [a-z ]+?):\t?(?P<value>.*)$")
_BOOL_LINE_RE = re.compile(r"^(?P<key>[A-Za-z0-9 ]+?):\t?(?P<value>.*)$")

# OSPF neighbor table rows: "<rid> \t<pri>\t<state>/<pos>\t<dtime>\t<iface> <ip>"
_NEIGH_ROW_RE = re.compile(
    r"^(?P<rid>\S+)\s*\t\s*(?P<pri>\d+)\s*\t\s*(?P<state>\S+)/(?P<pos>\S+?)\s*\t\s*"
    r"(?P<dtime>\S+)\s*\t\s*(?P<iface>\S+)\s+(?P<ip>\S+)\s*$",
)

# Interface block header: "Interface eth0 (21.0.22.68/32)" or "Virtual link ..."
_IFACE_HEADER_RE = re.compile(r"^(?:Interface|Virtual link)\s+(?P<name>\S+)")
_IFACE_KV_RE = re.compile(r"^(?P<key>[A-Za-z0-9 ()]+?):\s+(?P<value>\S.*)$")

# LSADB scope headers and rows.
_LSADB_SCOPE_RE = re.compile(r"^(?:Global|Area\s+\S+|Link\s+\S+)\s*$")
_LSADB_ROW_RE = re.compile(
    r"^(?P<type>[0-9a-fA-F]+)\s+(?P<lsid>\S+)\s+(?P<router>\S+)\s+"
    r"(?P<seq>\S+)\s+(?P<age>\d+)\s+(?P<sum>\S+)\s*$",
)


def _section_lines(result: CommandResult, code: int) -> list[str]:
    section = result.section(code)
    if section is None:
        return []
    return [ln.text for ln in section.lines]


def _header(lines: list[str]) -> tuple[str, bool]:
    """Extract (protocol name, up flag) from the first line."""
    if not lines:
        return "", True
    first = lines[0].strip()
    m = _NOT_UP_RE.match(first)
    if m:
        return m.group("name"), False
    m = _PROTOCOL_HEADER_RE.match(first)
    if m:
        return m.group("name"), True
    return first, True


def _to_bool(value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered in ("yes", "enabled", "true"):
        return True
    if lowered in ("no", "disabled", "false"):
        return False
    return None


def _to_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# show ospf
# ---------------------------------------------------------------------------


def parse_ospf(result: CommandResult) -> OspfInfo:
    """Decode a ``show ospf`` response."""
    lines = _section_lines(result, 1014)
    protocol, up = _header(lines)

    rfc1583: bool | None = None
    stub_router: bool | None = None
    tick: int | None = None
    num_areas: int | None = None
    num_lsas: int | None = None
    areas: list[OspfArea] = []

    current: dict[str, object] | None = None
    current_networks: list[str] = []
    current_extra: list[str] = []

    def _flush() -> None:
        nonlocal current, current_networks, current_extra
        if current is not None:
            areas.append(
                OspfArea(
                    area_id=str(current["area_id"]),
                    backbone=bool(current["backbone"]),
                    stub=current.get("stub"),  # type: ignore[arg-type]
                    nssa=current.get("nssa"),  # type: ignore[arg-type]
                    transit=current.get("transit"),  # type: ignore[arg-type]
                    interfaces_count=current.get("interfaces_count"),  # type: ignore[arg-type]
                    neighbors_count=current.get("neighbors_count"),  # type: ignore[arg-type]
                    adjacent_count=current.get("adjacent_count"),  # type: ignore[arg-type]
                    networks=list(current_networks),
                    extra_lines=list(current_extra),
                )
            )
        current = None
        current_networks = []
        current_extra = []

    for raw in lines[1:]:
        text = raw.strip()
        if not text:
            continue
        m = _AREA_HEADER_RE.match(text)
        if m:
            _flush()
            current = {
                "area_id": m.group("id"),
                "backbone": bool(m.group("extra") and "BACKBONE" in m.group("extra")),
            }
            continue
        if current is not None:
            # Area sub-fields: "Stub:\tNo", "Number of interfaces:\t1",
            # "Area networks:" headers and their "<net>\t<adv>\t<active>" rows.
            m = _AREA_FIELD_RE.match(text)
            if m:
                key, value = m.group("key"), m.group("value").strip()
                if key == "Number of interfaces":
                    current["interfaces_count"] = _to_int(value)
                elif key == "Number of neighbors":
                    current["neighbors_count"] = _to_int(value)
                elif key == "Number of adjacent neighbors":
                    current["adjacent_count"] = _to_int(value)
                continue
            if text.startswith(("Stub:", "NSSA:", "Transit:")):
                key, _, value = text.partition(":")
                flag = _to_bool(value.replace("\t", " "))
                if key == "Stub":
                    current["stub"] = flag
                elif key == "NSSA":
                    current["nssa"] = flag
                else:
                    current["transit"] = flag
                continue
            if text.startswith(("Area networks:", "Area external networks:")):
                current_networks.append(text)
                continue
            current_networks.append(text)
            continue
        # Protocol-level fields.
        m = _BOOL_LINE_RE.match(text)
        if m:
            key, value = m.group("key").strip(), m.group("value").strip()
            if key == "RFC1583 compatibility":
                rfc1583 = _to_bool(value)
            elif key == "Stub router":
                stub_router = _to_bool(value)
            elif key == "RT scheduler tick":
                tick = _to_int(value)
            elif key == "Number of areas":
                num_areas = _to_int(value)
            elif key.startswith("Number of LSAs"):
                num_lsas = _to_int(value)
    _flush()

    return OspfInfo(
        raw_text=result.raw_text,
        command=result.command,
        protocol=protocol,
        up=up,
        rfc1583=rfc1583,
        stub_router=stub_router,
        tick=tick,
        num_areas=num_areas,
        num_lsas=num_lsas,
        areas=areas,
    )


# ---------------------------------------------------------------------------
# show ospf neighbors
# ---------------------------------------------------------------------------


def parse_ospf_neighbors(result: CommandResult) -> OspfNeighborsResult:
    """Decode a ``show ospf neighbors`` response."""
    lines = _section_lines(result, 1013)
    protocol, up = _header(lines)
    rows: list[OspfNeighborRow] = []
    for raw in lines[1:]:
        m = _NEIGH_ROW_RE.match(raw.strip())
        if m:
            rows.append(
                OspfNeighborRow(
                    router_id=m.group("rid"),
                    priority=_to_int(m.group("pri")),
                    state=m.group("state"),
                    position=m.group("pos"),
                    dead_timer=m.group("dtime"),
                    interface=m.group("iface"),
                    router_ip=m.group("ip"),
                ),
            )
    return OspfNeighborsResult(
        raw_text=result.raw_text,
        command=result.command,
        protocol=protocol,
        up=up,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# show ospf interface
# ---------------------------------------------------------------------------


def parse_ospf_interfaces(result: CommandResult) -> OspfInterfacesResult:
    """Decode a ``show ospf interface`` response."""
    lines = _section_lines(result, 1015)
    protocol, up = _header(lines)
    interfaces: list[OspfInterface] = []
    current: OspfInterface | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None:
            interfaces.append(current)
            current = None

    for raw in lines[1:]:
        text = raw.strip()
        if not text:
            continue
        m = _IFACE_HEADER_RE.match(text)
        if m:
            _flush()
            current = OspfInterface(
                name=m.group("name"),
                header=text,
                fields={},
                extra_lines=[],
            )
            continue
        if current is None:
            continue
        m = _IFACE_KV_RE.match(text)
        if m:
            key = m.group("key").strip()
            value = m.group("value").strip()
            # First occurrence wins for duplicated keys (top-level block only).
            current.fields.setdefault(key, value)
        else:
            current.extra_lines.append(text)
    _flush()

    return OspfInterfacesResult(
        raw_text=result.raw_text,
        command=result.command,
        protocol=protocol,
        up=up,
        interfaces=interfaces,
    )


# ---------------------------------------------------------------------------
# show ospf state / topology
# ---------------------------------------------------------------------------


def parse_ospf_state(result: CommandResult) -> OspfStateResult:
    """Decode a ``show ospf state`` / ``show ospf topology`` response."""
    lines = _section_lines(result, 1016)
    protocol, up = _header(lines)
    areas: list[str] = []
    routers: list[str] = []
    body: list[str] = []
    for raw in lines[1:]:
        text = raw.rstrip()
        if not text.strip():
            continue
        stripped = text.strip()
        body.append(text)
        if stripped.startswith("area ") and " " not in stripped[len("area "):]:
            areas.append(stripped)
        elif stripped.startswith("router ") and " " not in stripped[len("router "):]:
            routers.append(stripped)
    return OspfStateResult(
        raw_text=result.raw_text,
        command=result.command,
        protocol=protocol,
        up=up,
        areas=areas,
        routers=routers,
        lines=body,
    )


# ---------------------------------------------------------------------------
# show ospf lsadb
# ---------------------------------------------------------------------------


def parse_ospf_lsadb(result: CommandResult) -> OspfLsadbResult:
    """Decode a ``show ospf lsadb`` response."""
    lines = _section_lines(result, 1017)
    protocol, up = _header(lines)
    scopes: list[OspfLsadbScope] = []
    current: OspfLsadbScope | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None:
            scopes.append(current)
            current = None

    for raw in lines[1:]:
        text = raw.strip()
        if not text:
            continue
        if _LSADB_SCOPE_RE.match(text):
            _flush()
            current = OspfLsadbScope(scope=text, rows=[])
            continue
        if current is None or text.startswith("Type "):
            # Column header ("Type   LS ID ...") before any scope, or stray.
            continue
        m = _LSADB_ROW_RE.match(text)
        if m:
            current.rows.append(
                OspfLsaRow(
                    type=m.group("type"),
                    ls_id=m.group("lsid"),
                    router=m.group("router"),
                    sequence=m.group("seq"),
                    age=_to_int(m.group("age")),
                    checksum=m.group("sum"),
                ),
            )
    _flush()

    return OspfLsadbResult(
        raw_text=result.raw_text,
        command=result.command,
        protocol=protocol,
        up=up,
        scopes=scopes,
    )
