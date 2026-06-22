"""
Shared OPC-UA security helpers (server and client).

Holds the pieces both plugin roles need:
- SECURITY_POLICY_MAPPING: config string -> opcua-asyncio SecurityPolicy class.
- SECURITY_MODE_MAPPING: config string -> MessageSecurityMode int.
- get_local_ip_addresses(): enumerate local IPs for certificate SANs.
- generate_certificate_with_sans(): self-signed cert generation.

The Server's OpcuaSecurityManager imports these; the Client's
client_security module uses SECURITY_POLICY_MAPPING / SECURITY_MODE_MAPPING
to drive asyncua Client.set_security(), and generate_certificate_with_sans
to mint a client application certificate when none is supplied.
"""

import datetime
import ipaddress
import os
import socket
from pathlib import Path
from typing import List, Set

from asyncua.crypto.security_policies import (
    SecurityPolicyAes128Sha256RsaOaep,
    SecurityPolicyAes256Sha256RsaPss,
    SecurityPolicyBasic256Sha256,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# Import logging (handle both package and direct loading)
try:
    from .opcua_logging import log_debug, log_error, log_warn
except ImportError:
    from opcua_logging import log_debug, log_error, log_warn


# Mapping from config strings to opcua-asyncio security policy classes.
SECURITY_POLICY_MAPPING = {
    "None": None,
    "Basic256Sha256": SecurityPolicyBasic256Sha256,
    "Aes128_Sha256_RsaOaep": SecurityPolicyAes128Sha256RsaOaep,
    "Aes256_Sha256_RsaPss": SecurityPolicyAes256Sha256RsaPss,
}

# Mapping from config strings to opcua-asyncio message security modes.
SECURITY_MODE_MAPPING = {
    "None": 1,  # MessageSecurityMode.None_
    "Sign": 2,  # MessageSecurityMode.Sign
    "SignAndEncrypt": 3,  # MessageSecurityMode.SignAndEncrypt
}


# ioctl constants for network interface enumeration (Linux)
_SIOCGIFCONF = 0x8912  # ioctl request code to get interface configuration
_SIZEOF_IFREQ = 40  # sizeof(struct ifreq) on 64-bit Linux
_MAX_INTERFACES = 128  # Maximum number of network interfaces to query


def get_local_ip_addresses() -> Set[str]:
    """
    Get all local IP addresses of the machine.

    Returns:
        Set of IP address strings (both IPv4 and IPv6)
    """
    ip_addresses = set()

    # Always include localhost addresses
    ip_addresses.add("127.0.0.1")
    ip_addresses.add("::1")

    try:
        # Method 1: Get IPs from all network interfaces
        hostname = socket.gethostname()
        try:
            # Get all addresses associated with hostname
            for info in socket.getaddrinfo(hostname, None):
                ip = info[4][0]
                # Filter out link-local addresses using ipaddress module
                try:
                    addr = ipaddress.ip_address(ip)
                    if not addr.is_link_local:
                        ip_addresses.add(ip)
                except ValueError:
                    pass
        except socket.gaierror:
            pass

        # Method 2: Connect to external address to find default interface IP
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                # Doesn't actually connect, just determines route
                s.connect(("8.8.8.8", 80))
                ip_addresses.add(s.getsockname()[0])
        except Exception:
            pass

        # Method 3: Try to get all interface IPs using netifaces-like approach
        try:
            import array
            import fcntl
            import struct

            # Get list of network interfaces
            buf_size = _MAX_INTERFACES * _SIZEOF_IFREQ
            buf = array.array("B", b"\0" * buf_size)

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                result = fcntl.ioctl(
                    s.fileno(),
                    _SIOCGIFCONF,
                    struct.pack("iL", buf_size, buf.buffer_info()[0]),
                )
                out_bytes = struct.unpack("iL", result)[0]

                # Parse the buffer for interface addresses
                offset = 0
                while offset < out_bytes:
                    # Interface name is 16 bytes, then sockaddr (unused, skip it)
                    # Skip to IP address (offset 20 from start of entry)
                    ip_offset = offset + 20
                    if ip_offset + 4 <= len(buf):
                        ip_bytes = buf[ip_offset : ip_offset + 4].tobytes()
                        ip = socket.inet_ntoa(ip_bytes)
                        if ip != "0.0.0.0":
                            ip_addresses.add(ip)
                    offset += _SIZEOF_IFREQ
        except Exception:
            pass

    except Exception as e:
        log_warn(f"Error getting local IP addresses: {e}")

    return ip_addresses


def generate_certificate_with_sans(
    cert_path: Path,
    key_path: Path,
    app_uri: str,
    dns_names: List[str],
    ip_addresses: List[str],
    common_name: str = "OpenPLC OPC-UA",
    organization: str = "Autonomy Logic",
    country: str = "US",
    state: str = "CA",
    locality: str = "California",
    key_size: int = 2048,
    valid_days: int = 3650,
) -> bool:
    """
    Generate a self-signed certificate with multiple Subject Alternative Names.

    Suitable for OPC-UA servers and clients: includes the application URI
    (required by OPC-UA), plus DNS names and IP addresses. Both SERVER_AUTH
    and CLIENT_AUTH extended key usages are set so the same routine works for
    either role.

    Args:
        cert_path: Path where certificate will be saved (PEM format)
        key_path: Path where private key will be saved (PEM format)
        app_uri: Application URI for the certificate
        dns_names: List of DNS names to include in SAN
        ip_addresses: List of IP addresses to include in SAN
        common_name: Certificate common name
        organization: Organization name
        country: Country code
        state: State/Province
        locality: City/Locality
        key_size: RSA key size (default 2048)
        valid_days: Certificate validity in days (default 3650 = 10 years)

    Returns:
        bool: True if certificate generated successfully
    """
    try:
        # Generate RSA private key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=key_size,
        )

        # Build subject name
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, country),
                x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, state),
                x509.NameAttribute(NameOID.LOCALITY_NAME, locality),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
                x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            ]
        )

        # Build Subject Alternative Names
        san_entries = []

        # Add URI (required for OPC-UA)
        san_entries.append(x509.UniformResourceIdentifier(app_uri))

        # Add DNS names
        for dns_name in dns_names:
            if dns_name:  # Skip empty strings
                san_entries.append(x509.DNSName(dns_name))

        # Add IP addresses
        for ip_str in ip_addresses:
            if ip_str:  # Skip empty strings
                try:
                    ip_obj = ipaddress.ip_address(ip_str)
                    san_entries.append(x509.IPAddress(ip_obj))
                except ValueError as e:
                    log_warn(f"Invalid IP address '{ip_str}' for SAN: {e}")

        # Build certificate
        now = datetime.datetime.now(datetime.timezone.utc)
        cert_builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=valid_days))
            .add_extension(
                x509.SubjectAlternativeName(san_entries),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    content_commitment=True,  # nonRepudiation - required by OPC-UA
                    data_encipherment=True,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        ExtendedKeyUsageOID.SERVER_AUTH,
                        ExtendedKeyUsageOID.CLIENT_AUTH,
                    ]
                ),
                critical=False,
            )
        )

        # Sign the certificate
        certificate = cert_builder.sign(private_key, hashes.SHA256())

        # Write private key (PKCS8 format required by asyncua) with 0600 perms
        key_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(
                private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )

        # Write certificate to file
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cert_path, "wb") as f:
            f.write(certificate.public_bytes(serialization.Encoding.PEM))

        log_debug(f"Generated certificate with {len(san_entries)} SAN entries")
        log_debug(f"  DNS names: {dns_names}")
        log_debug(f"  IP addresses: {ip_addresses}")
        log_debug(f"  URI: {app_uri}")

        return True

    except Exception as e:
        log_error(f"Failed to generate certificate: {e}")
        return False
