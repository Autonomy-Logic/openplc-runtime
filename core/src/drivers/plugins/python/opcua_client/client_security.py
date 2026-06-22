"""
Client-side OPC-UA security: translate a RemoteEndpointSecurity config into
asyncua Client.set_security() / user-token calls.

Reuses the shared security maps and self-signed certificate generator. When a
secure policy is requested without a client application certificate, one is
minted (self-signed) under <plugin_dir>/certs/client_<server>/.
"""

import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from asyncua import Client, ua

_current_dir = os.path.dirname(os.path.abspath(__file__))
_python_dir = os.path.dirname(_current_dir)
for _p in (_current_dir, _python_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared.opcua_common.opcua_security_common import (  # noqa: E402
    SECURITY_MODE_MAPPING,
    SECURITY_POLICY_MAPPING,
    generate_certificate_with_sans,
    get_local_ip_addresses,
)

try:
    from .opcua_client_logging import log_debug, log_error, log_info
except ImportError:
    from opcua_client_logging import log_debug, log_error, log_info


# Application URI baked into the client certificate. Must match the URI the
# client advertises (set on the asyncua Client below) or servers reject the
# session with BadCertificateUriInvalid.
CLIENT_APP_URI = "urn:autonomy-logic:openplc:opcua:client"


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name) or "server"


def _write_pem(path: Path, pem: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(pem)


def _ensure_client_certificate(security, certs_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """Return (cert_path, key_path), materializing the configured PEMs or
    minting a self-signed application certificate when none is supplied."""
    cert_path = certs_dir / "client_cert.pem"
    key_path = certs_dir / "client_key.pem"

    if security.client_cert_pem and security.client_key_pem:
        _write_pem(cert_path, security.client_cert_pem)
        _write_pem(key_path, security.client_key_pem)
        return cert_path, key_path

    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    hostname = "openplc-client"
    try:
        import socket

        hostname = socket.gethostname() or hostname
    except Exception:
        pass

    ip_addresses = list(get_local_ip_addresses())
    ok = generate_certificate_with_sans(
        cert_path=cert_path,
        key_path=key_path,
        app_uri=CLIENT_APP_URI,
        dns_names=[hostname, "localhost"],
        ip_addresses=ip_addresses,
        common_name="OpenPLC OPC-UA Client",
    )
    if not ok:
        return None, None
    return cert_path, key_path


def _apply_auth(client: Client, security) -> None:
    """Apply the user-identity token (anonymous / username / certificate)."""
    if security.auth_mode == "username" and security.username:
        client.set_user(security.username)
        if security.password is not None:
            client.set_password(security.password)
    # "certificate" auth relies on the application certificate loaded via
    # set_security(); "anonymous" needs nothing.


async def apply_client_security(
    client: Client, security, plugin_dir: str, server_name: str
) -> bool:
    """Configure the asyncua Client's secure channel + user token from config.

    Returns False if the requested policy is unsupported or a required client
    certificate could not be obtained.
    """
    # Advertise an application URI that matches our certificate SAN.
    try:
        client.application_uri = CLIENT_APP_URI
    except Exception:
        pass

    if security.security_policy == "None" and security.security_mode == "None":
        # Plain channel; only a user token (if any) applies.
        _apply_auth(client, security)
        return True

    policy_cls = SECURITY_POLICY_MAPPING.get(security.security_policy)
    if policy_cls is None:
        log_error(f"Unsupported security policy '{security.security_policy}' for '{server_name}'")
        return False

    mode_int = SECURITY_MODE_MAPPING.get(security.security_mode)
    if mode_int is None:
        log_error(f"Unsupported security mode '{security.security_mode}' for '{server_name}'")
        return False
    mode = ua.MessageSecurityMode(mode_int)

    certs_dir = Path(plugin_dir) / "certs" / f"client_{_safe_name(server_name)}"
    cert_path, key_path = _ensure_client_certificate(security, certs_dir)
    if cert_path is None or key_path is None:
        log_error(f"Could not obtain client certificate for '{server_name}'")
        return False

    server_cert_path: Optional[str] = None
    if security.server_cert_pem:
        scp = certs_dir / "server_cert.pem"
        _write_pem(scp, security.server_cert_pem)
        server_cert_path = str(scp)

    try:
        await client.set_security(
            policy_cls,
            str(cert_path),
            str(key_path),
            server_certificate=server_cert_path,
            mode=mode,
        )
    except Exception as e:
        log_error(f"set_security failed for '{server_name}': {e}")
        return False

    _apply_auth(client, security)
    log_info(
        f"Security applied for '{server_name}': "
        f"{security.security_policy}/{security.security_mode}, auth={security.auth_mode}"
    )
    log_debug(f"Client certificate: {cert_path}")
    return True
