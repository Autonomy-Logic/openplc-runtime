"""Who is allowed to change this runtime's version, published at
``GET /api/capabilities`` as ``updatePolicy``.

The runtime never updates itself. It only reports which mechanism owns that
job, so a client can offer the right action instead of a button that cannot
work. Resolution order:

  1. ``OPENPLC_UPDATE_POLICY`` -- explicit, and wins outright. The sidecar
     bootloader sets ``self`` when it creates the runtime container. An OEM
     shipping a vendor-managed device sets ``none``.
  2. Running in a container with no override -> ``managed``. Something else
     created this container, and whatever created it chose the image tag --
     which IS the version. An orchestrator-managed vPLC lands here.
  3. Otherwise -> ``manual``. A native source install, updated from a shell.

This is deliberately capability-based rather than identity-based: we report
what the deployment CAN do, never a guess at what it IS. Only our own sidecar
sets ``self``, so a false positive is impossible -- an orchestrator vPLC never
runs our installer and never receives that variable. Getting this backwards
(sniffing for orchestrator-shaped networks or cgroup patterns) would be a
guess that can be wrong in both directions.

Clients that predate this field see it missing and must treat that as "no
update support", which is exactly the behaviour they had before.
"""

import os
import platform
import socket
from typing import Optional

from webserver.config import is_running_in_container

# The sidecar owns the container spec and may replace the image (RTOP-283).
POLICY_SELF: str = "self"
# Some other supervisor owns the container; it must perform the swap.
POLICY_MANAGED: str = "managed"
# Native install: a human with a shell owns it.
POLICY_MANUAL: str = "manual"
# Vendor-locked. Set by an OEM that ships its own update channel.
POLICY_NONE: str = "none"

VALID_POLICIES: frozenset[str] = frozenset(
    {POLICY_SELF, POLICY_MANAGED, POLICY_MANUAL, POLICY_NONE}
)

# Port the sidecar's control API listens on. Reported so a client does not
# have to hard-code it; the sidecar passes the real value when it differs.
DEFAULT_SIDECAR_PORT: int = 8445


def _resolve_update_policy() -> str:
    """Return the update policy for this deployment. See module docstring."""
    override = os.getenv("OPENPLC_UPDATE_POLICY", "").strip().lower()
    if override in VALID_POLICIES:
        return override

    # An unrecognised override is a deployment error, not a reason to guess a
    # permissive answer -- fall through to detection rather than trusting it.
    if is_running_in_container():
        return POLICY_MANAGED

    return POLICY_MANUAL


def _resolve_sidecar_port(policy: str) -> Optional[int]:
    """Port of the managing sidecar, or ``None`` when there is not one.

    Only meaningful under ``self``: every other policy means no sidecar of
    ours is listening, and publishing a port nothing answers on would send
    clients somewhere useless.
    """
    if policy != POLICY_SELF:
        return None

    raw = os.getenv("OPENPLC_SIDECAR_PORT", "").strip()
    if not raw:
        return DEFAULT_SIDECAR_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_SIDECAR_PORT
    if not 1 <= port <= 65535:
        return DEFAULT_SIDECAR_PORT
    return port


UPDATE_POLICY: str = _resolve_update_policy()
SIDECAR_PORT: Optional[int] = _resolve_sidecar_port(UPDATE_POLICY)


def device_info() -> dict[str, object]:
    """Host facts for the editor's Runtime Status header.

    Served from an authenticated route, unlike ``updatePolicy`` itself: the
    policy has to be readable before login so a client can decide what to
    offer, whereas kernel and architecture are only ever shown to somebody
    already looking at a device they can log in to.

    ``hostname`` is the one field that is also broadcast unauthenticated (the
    discovery responder already publishes it), so nothing here widens what an
    unauthenticated observer on the LAN can learn.
    """
    return {
        "hostname": socket.gethostname(),
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "system": platform.system(),
        "containerized": is_running_in_container(),
        "updatePolicy": UPDATE_POLICY,
        "sidecarPort": SIDECAR_PORT,
    }
