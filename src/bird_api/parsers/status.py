"""Parser for the ``show status`` command.

Wire example (BIRD 3.1.7)::

    1000-BIRD 3.1.7
    1011-Router ID is 10.0.0.1
     Hostname is c-6a8c3c11-145d6674-da1004003283
     Current server time is 2026-08-24 13:07:54.017
     Last reboot on 2026-08-24 13:07:46.670
     Last reconfiguration on 2026-08-24 13:07:46.670
    0013 Daemon is up and running

Section 1000 carries ``BIRD <version>``; section 1011 carries the router
status fields (router ID, hostname, server time, reboot/reconfig times,
and optional config-undo timer). The final line (code 0013) carries the
runtime state.
"""

from __future__ import annotations

import re

from ..models import Status
from ..protocol import CommandResult
from ._base import parse_bird_timestamp

__all__ = ["parse_status"]

# Match: "Configuration unconfirmed, undo in 4 s"
_UNDO_RE = re.compile(r"undo in\s+(\d+)\s+s")


def parse_status(result: CommandResult) -> Status:
    """Decode a ``show status`` response into :class:`~bird_api.models.Status`."""
    bird_version = ""
    section_1000 = result.section(1000)
    if section_1000 is not None and section_1000.lines:
        first = section_1000.lines[0].text
        # Expected form: "BIRD 3.1.7"
        if first.startswith("BIRD "):
            bird_version = first[len("BIRD ") :].strip()

    router_id = ""
    hostname = ""
    current_server_time = None
    last_reboot = None
    last_reconfiguration = None
    config_undo_in: int | None = None

    section_1011 = result.section(1011)
    if section_1011 is not None:
        for ln in section_1011.lines:
            text = ln.text
            if text.startswith("Router ID is "):
                router_id = text[len("Router ID is ") :].strip()
            elif text.startswith("Hostname is "):
                hostname = text[len("Hostname is ") :].strip()
            elif text.startswith("Current server time is "):
                current_server_time = parse_bird_timestamp(text[len("Current server time is ") :])
            elif text.startswith("Last reboot on "):
                last_reboot = parse_bird_timestamp(text[len("Last reboot on ") :])
            elif text.startswith("Last reconfiguration on "):
                last_reconfiguration = parse_bird_timestamp(text[len("Last reconfiguration on ") :])
            elif "undo in" in text:
                m = _UNDO_RE.search(text)
                if m:
                    config_undo_in = int(m.group(1))

    # Final line carries the state. 0013 = "Daemon is up and running" /
    # "Shutdown in progress" / "Reconfiguration in progress".
    state_text = result.final_text.strip()
    state_lower = state_text.lower()
    if "shutdown" in state_lower:
        reconfig_state = "shutdown"
    elif "reconfig" in state_lower and "running" not in state_lower:
        reconfig_state = "reconfig"
    else:
        reconfig_state = "up"

    return Status(
        raw_text=result.raw_text,
        command=result.command,
        bird_version=bird_version,
        router_id=router_id,
        hostname=hostname,
        current_server_time=current_server_time,
        last_reboot=last_reboot,
        last_reconfiguration=last_reconfiguration,
        reconfig_state=reconfig_state,
        config_undo_in=config_undo_in,
    )
