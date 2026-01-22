"""
OPC-UA Security Utilities

This module provides utilities for handling OPC-UA security features including:
- Auto-generation of server certificates
- Certificate loading and validation
- Security policy and mode mapping
- Client trust list management
"""

import datetime
import hashlib
import ipaddress
import os
import shutil
import socket
import ssl
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Set, Tuple
from urllib.parse import urlparse

from asyncua import ua
from asyncua.crypto.permission_rules import PermissionRuleset
from asyncua.crypto.security_policies import (
    SecurityPolicyAes128Sha256RsaOaep,
    SecurityPolicyAes256Sha256RsaPss,
    SecurityPolicyBasic256Sha256,
)
from asyncua.crypto.truststore import TrustStore
from asyncua.crypto.validator import CertificateValidator
from asyncua.server.user_managers import UserRole
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# Add directories to path for module access
_current_dir = os.path.dirname(os.path.abspath(__file__))
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

# Import logging (handle both package and direct loading)
try:
    from .opcua_logging import log_error, log_info, log_warn
except ImportError:
    from opcua_logging import log_error, log_info, log_warn


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
    common_name: str = "OpenPLC OPC-UA Server",
    organization: str = "Autonomy Logic",
    country: str = "US",
    state: str = "CA",
    locality: str = "California",
    key_size: int = 2048,
    valid_days: int = 3650,
) -> bool:
    """
    Generate a self-signed certificate with multiple Subject Alternative Names.

    This function creates a certificate suitable for OPC-UA servers with proper
    SAN extensions including multiple DNS names, IP addresses, and URIs.

    The default validity period is 10 years (3650 days) to minimize certificate
    renewal overhead in industrial/embedded environments where PLCs may run
    for extended periods without maintenance.

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

        # Write private key to file with restricted permissions (PKCS8 format required by asyncua)
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

        log_info(f"Generated certificate with {len(san_entries)} SAN entries")
        log_info(f"  DNS names: {dns_names}")
        log_info(f"  IP addresses: {ip_addresses}")
        log_info(f"  URI: {app_uri}")

        return True

    except Exception as e:
        log_error(f"Failed to generate certificate: {e}")
        return False


class OpenPLCRoleRuleset(PermissionRuleset):
    """
    Custom permission ruleset for OpenPLC OPC-UA server.

    Extends the standard SimpleRoleRuleset to allow regular users to
    modify subscription parameters (publish interval, etc.).

    The default asyncua SimpleRoleRuleset is missing ModifySubscriptionRequest
    from the USER_TYPES list, which prevents non-admin users from modifying
    subscription parameters via SCADA clients like UAExpert.
    """

    # Operations that require Admin role
    ADMIN_TYPES = [
        ua.ObjectIds.RegisterServerRequest_Encoding_DefaultBinary,
        ua.ObjectIds.RegisterServer2Request_Encoding_DefaultBinary,
        ua.ObjectIds.AddNodesRequest_Encoding_DefaultBinary,
        ua.ObjectIds.DeleteNodesRequest_Encoding_DefaultBinary,
        ua.ObjectIds.AddReferencesRequest_Encoding_DefaultBinary,
        ua.ObjectIds.DeleteReferencesRequest_Encoding_DefaultBinary,
    ]

    # Operations allowed for regular User role (includes ModifySubscription)
    USER_TYPES = [
        ua.ObjectIds.CreateSessionRequest_Encoding_DefaultBinary,
        ua.ObjectIds.CloseSessionRequest_Encoding_DefaultBinary,
        ua.ObjectIds.ActivateSessionRequest_Encoding_DefaultBinary,
        ua.ObjectIds.ReadRequest_Encoding_DefaultBinary,
        ua.ObjectIds.WriteRequest_Encoding_DefaultBinary,
        ua.ObjectIds.BrowseRequest_Encoding_DefaultBinary,
        ua.ObjectIds.GetEndpointsRequest_Encoding_DefaultBinary,
        ua.ObjectIds.FindServersRequest_Encoding_DefaultBinary,
        ua.ObjectIds.TranslateBrowsePathsToNodeIdsRequest_Encoding_DefaultBinary,
        ua.ObjectIds.CreateSubscriptionRequest_Encoding_DefaultBinary,
        ua.ObjectIds.ModifySubscriptionRequest_Encoding_DefaultBinary,  # Added for SCADA clients
        ua.ObjectIds.DeleteSubscriptionsRequest_Encoding_DefaultBinary,
        ua.ObjectIds.CreateMonitoredItemsRequest_Encoding_DefaultBinary,
        ua.ObjectIds.ModifyMonitoredItemsRequest_Encoding_DefaultBinary,
        ua.ObjectIds.DeleteMonitoredItemsRequest_Encoding_DefaultBinary,
        ua.ObjectIds.HistoryReadRequest_Encoding_DefaultBinary,
        ua.ObjectIds.PublishRequest_Encoding_DefaultBinary,
        ua.ObjectIds.RepublishRequest_Encoding_DefaultBinary,
        ua.ObjectIds.CloseSecureChannelRequest_Encoding_DefaultBinary,
        ua.ObjectIds.CallRequest_Encoding_DefaultBinary,
        ua.ObjectIds.SetMonitoringModeRequest_Encoding_DefaultBinary,
        ua.ObjectIds.SetPublishingModeRequest_Encoding_DefaultBinary,
        ua.ObjectIds.RegisterNodesRequest_Encoding_DefaultBinary,
        ua.ObjectIds.UnregisterNodesRequest_Encoding_DefaultBinary,
        ua.ObjectIds.TransferSubscriptionsRequest_Encoding_DefaultBinary,  # Added for session transfer
    ]

    def __init__(self):
        """Initialize the permission ruleset with role-based permissions."""
        admin_ids = list(map(ua.NodeId, self.ADMIN_TYPES))
        user_ids = list(map(ua.NodeId, self.USER_TYPES))
        self._permission_dict = {
            UserRole.Admin: set().union(admin_ids, user_ids),
            UserRole.User: set(user_ids),
            UserRole.Anonymous: set(),  # Anonymous users have no permissions by default
        }

    def check_validity(self, user, action_type_id, body):
        """
        Check if user has permission for the given action.

        Args:
            user: User object with role attribute
            action_type_id: NodeId of the requested operation
            body: Request body (unused, for future extensions)

        Returns:
            True if user has permission, False otherwise
        """
        if action_type_id in self._permission_dict.get(user.role, set()):
            return True
        return False


class OpcuaSecurityManager:
    """Manages OPC-UA security configuration and certificates."""

    # Mapping from config strings to opcua-asyncio security policies
    SECURITY_POLICY_MAPPING = {
        "None": None,
        "Basic256Sha256": SecurityPolicyBasic256Sha256,
        "Aes128_Sha256_RsaOaep": SecurityPolicyAes128Sha256RsaOaep,
        "Aes256_Sha256_RsaPss": SecurityPolicyAes256Sha256RsaPss,
    }

    # Mapping from config strings to opcua-asyncio message security modes
    SECURITY_MODE_MAPPING = {
        "None": 1,  # MessageSecurityMode.None
        "Sign": 2,  # MessageSecurityMode.Sign
        "SignAndEncrypt": 3,  # MessageSecurityMode.SignAndEncrypt
    }

    # Mapping from (policy, mode) to SecurityPolicyType for asyncua Server
    POLICY_TYPE_MAPPING = {
        ("None", "None"): ua.SecurityPolicyType.NoSecurity,
        ("Basic256Sha256", "Sign"): ua.SecurityPolicyType.Basic256Sha256_Sign,
        ("Basic256Sha256", "SignAndEncrypt"): ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
        ("Basic256", "Sign"): ua.SecurityPolicyType.Basic256_Sign,
        ("Basic256", "SignAndEncrypt"): ua.SecurityPolicyType.Basic256_SignAndEncrypt,
        ("Basic128Rsa15", "Sign"): ua.SecurityPolicyType.Basic128Rsa15_Sign,
        ("Basic128Rsa15", "SignAndEncrypt"): ua.SecurityPolicyType.Basic128Rsa15_SignAndEncrypt,
        ("Aes128_Sha256_RsaOaep", "Sign"): ua.SecurityPolicyType.Aes128Sha256RsaOaep_Sign,
        (
            "Aes128_Sha256_RsaOaep",
            "SignAndEncrypt",
        ): ua.SecurityPolicyType.Aes128Sha256RsaOaep_SignAndEncrypt,
        ("Aes256_Sha256_RsaPss", "Sign"): ua.SecurityPolicyType.Aes256Sha256RsaPss_Sign,
        (
            "Aes256_Sha256_RsaPss",
            "SignAndEncrypt",
        ): ua.SecurityPolicyType.Aes256Sha256RsaPss_SignAndEncrypt,
    }

    CERTS_DIR = "certs"
    SERVER_CERT_FILE = "server_cert.pem"
    SERVER_KEY_FILE = "server_key.pem"

    def __init__(self, config, plugin_dir: str = None):
        """
        Initialize security manager with configuration.

        Args:
            config: OpcuaConfig instance with security settings
            plugin_dir: Directory where certificates are stored (defaults to plugin directory)
        """
        self.config = config
        self.plugin_dir = plugin_dir or os.path.dirname(__file__)
        self.certs_dir = os.path.join(self.plugin_dir, self.CERTS_DIR)
        self.certificate_data = None
        self.private_key_data = None
        self.security_policy = None
        self.security_mode = None
        self.trusted_certificates = []  # List of trusted client certificates
        self._trust_store_temp_dir = None  # Track temp dir for cleanup

    async def initialize_security(self) -> bool:
        """
        Initialize security settings based on configuration.

        Returns:
            bool: True if security initialized successfully
        """
        try:
            # Map security policy
            self.security_policy = self.SECURITY_POLICY_MAPPING.get(self.config.security_policy)
            if self.config.security_policy != "None" and self.security_policy is None:
                log_error(f"Unsupported security policy: {self.config.security_policy}")
                return False

            # Map security mode
            self.security_mode = self.SECURITY_MODE_MAPPING.get(self.config.security_mode)
            if self.security_mode is None:
                log_error(f"Unsupported security mode: {self.config.security_mode}")
                return False

            # Load certificates if required
            if self.config.security_policy != "None" or self.config.security_mode != "None":
                if not await self._ensure_server_certificates():
                    return False

            # Load trusted client certificates
            if self.config.client_auth.enabled:
                if not self._load_trusted_certificates():
                    return False

            log_info(
                f"Security initialized: policy={self.config.security_policy}, mode={self.config.security_mode}"
            )
            return True

        except Exception as e:
            log_error(f"Failed to initialize security: {e}")
            return False

    def _is_certificate_valid(self, cert_path: str) -> bool:
        """
        Check if a certificate file exists and is still valid (not expired).

        Args:
            cert_path: Path to the certificate file

        Returns:
            bool: True if certificate exists and is valid, False otherwise
        """
        if not os.path.exists(cert_path):
            return False

        try:
            with open(cert_path, "rb") as f:
                cert_data = f.read()

            cert = x509.load_pem_x509_certificate(cert_data)

            # Use timezone-aware datetime for comparison
            now_utc = datetime.datetime.now(datetime.timezone.utc)

            # Get certificate validity dates (prefer UTC versions if available)
            not_valid_after = getattr(cert, "not_valid_after_utc", None)
            if not_valid_after is None:
                not_valid_after = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)

            not_valid_before = getattr(cert, "not_valid_before_utc", None)
            if not_valid_before is None:
                not_valid_before = cert.not_valid_before.replace(tzinfo=datetime.timezone.utc)

            # Check if certificate is not yet valid
            if not_valid_before > now_utc:
                log_warn(f"Certificate {cert_path} is not yet valid")
                return False

            # Check if certificate has expired
            if not_valid_after < now_utc:
                log_warn(f"Certificate {cert_path} has expired")
                return False

            # Certificate is valid
            days_until_expiry = (not_valid_after - now_utc).days
            log_info(f"Certificate {cert_path} is valid for {days_until_expiry} more days")
            return True

        except Exception as e:
            log_warn(f"Failed to validate certificate {cert_path}: {e}")
            return False

    def _remove_certificate_files(self, cert_path: str, key_path: str) -> None:
        """
        Remove existing certificate and key files.

        Args:
            cert_path: Path to the certificate file
            key_path: Path to the private key file
        """
        for file_path in [cert_path, key_path]:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    log_info(f"Removed expired certificate file: {file_path}")
                except Exception as e:
                    log_warn(f"Failed to remove file {file_path}: {e}")

    async def _ensure_server_certificates(self) -> bool:
        """
        Ensure server certificates exist and are valid, generate if missing or expired.

        Returns:
            bool: True if certificates are available
        """
        try:
            # Create certs directory if it doesn't exist
            os.makedirs(self.certs_dir, exist_ok=True)

            cert_path = os.path.join(self.certs_dir, self.SERVER_CERT_FILE)
            key_path = os.path.join(self.certs_dir, self.SERVER_KEY_FILE)

            # Check if certificates already exist and are valid
            if os.path.exists(cert_path) and os.path.exists(key_path):
                if self._is_certificate_valid(cert_path):
                    log_info(f"Found valid server certificates in {self.certs_dir}")
                else:
                    log_info("Server certificate is expired or invalid, regenerating")
                    self._remove_certificate_files(cert_path, key_path)
                    if not await self.generate_server_certificate(cert_path, key_path):
                        return False
            else:
                log_info(f"Server certificates not found, generating new ones in {self.certs_dir}")
                if not await self.generate_server_certificate(cert_path, key_path):
                    return False

            # Load the certificates
            return self._load_certificates(cert_path, key_path)

        except Exception as e:
            log_error(f"Failed to ensure server certificates: {e}")
            return False

    def _load_certificates(self, cert_path: str, key_path: str) -> bool:
        """
        Load certificate and private key files.

        Returns:
            bool: True if certificates loaded successfully
        """
        try:
            # Load certificate
            with open(cert_path, "rb") as cert_file:
                self.certificate_data = cert_file.read()

            # Load private key
            with open(key_path, "rb") as key_file:
                self.private_key_data = key_file.read()

            # Validate certificate format (basic check)
            if not self._validate_certificate_format():
                return False

            log_info(f"Server certificates loaded from {cert_path}")
            return True

        except FileNotFoundError as e:
            log_error(f"Certificate file not found: {e}")
            return False
        except Exception as e:
            log_error(f"Failed to load certificates: {e}")
            return False

    def _validate_certificate_format(self) -> bool:
        """
        Perform comprehensive validation of certificate format and extensions.

        Returns:
            bool: True if certificate format and extensions are valid
        """
        try:
            # Try to load certificate with ssl module for basic validation
            ssl.PEM_cert_to_DER_cert(self.certificate_data.decode("utf-8"))

            # Enhanced validation using cryptography library
            try:
                cert = x509.load_pem_x509_certificate(self.certificate_data)

                # Use timezone-aware datetime for comparison
                now_utc = datetime.datetime.now(datetime.timezone.utc)

                # Get certificate validity dates (prefer UTC versions if available)
                not_valid_after = getattr(cert, "not_valid_after_utc", None)
                if not_valid_after is None:
                    not_valid_after = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)

                not_valid_before = getattr(cert, "not_valid_before_utc", None)
                if not_valid_before is None:
                    not_valid_before = cert.not_valid_before.replace(tzinfo=datetime.timezone.utc)

                # Check if certificate is not yet valid
                if not_valid_before > now_utc:
                    log_warn("Certificate is not yet valid")
                    return False

                # Check expiration
                if not_valid_after < now_utc:
                    log_warn("Certificate has expired")
                    return False

                # Check if certificate will expire soon (within 30 days)
                days_until_expiry = (not_valid_after - now_utc).days
                if days_until_expiry < 30:
                    log_warn(f"Certificate expires in {days_until_expiry} days")

                # Check for Subject Alternative Name extension
                try:
                    san_ext = cert.extensions.get_extension_for_oid(
                        x509.ExtensionOID.SUBJECT_ALTERNATIVE_NAME
                    )
                    san_names = san_ext.value

                    # Log SAN entries for debugging
                    dns_names = [name.value for name in san_names if isinstance(name, x509.DNSName)]
                    ip_addresses = [
                        name.value.compressed
                        for name in san_names
                        if isinstance(name, x509.IPAddress)
                    ]
                    uris = [
                        name.value
                        for name in san_names
                        if isinstance(name, x509.UniformResourceIdentifier)
                    ]

                    log_info(f"Certificate SAN DNS names: {dns_names}")
                    log_info(f"Certificate SAN IP addresses: {ip_addresses}")
                    log_info(f"Certificate SAN URIs: {uris}")

                    # Check if we have expected entries
                    system_hostname = socket.gethostname()
                    if system_hostname not in dns_names and system_hostname != "localhost":
                        log_warn(
                            f"System hostname '{system_hostname}' not found in certificate DNS SANs"
                        )

                    # Check for application URI
                    expected_uri = "urn:autonomy-logic:openplc:opcua:server"
                    if expected_uri not in uris:
                        log_warn(
                            f"Expected application URI '{expected_uri}' not found in certificate"
                        )

                except x509.ExtensionNotFound:
                    log_warn("Certificate missing Subject Alternative Name extension")

                # Check key usage extensions
                try:
                    key_usage = cert.extensions.get_extension_for_oid(
                        x509.ExtensionOID.KEY_USAGE
                    ).value
                    if not key_usage.digital_signature:
                        log_warn("Certificate lacks digital signature key usage")
                    if not key_usage.key_encipherment:
                        log_warn("Certificate lacks key encipherment usage")
                except x509.ExtensionNotFound:
                    log_warn("Certificate missing key usage extension")

                log_info("Certificate format and extensions validated")
                return True

            except ImportError:
                log_warn("cryptography library not available for enhanced validation")
                return True  # Fall back to basic validation

        except Exception:
            try:
                # Try as DER format
                ssl.DER_cert_to_PEM_cert(self.certificate_data)
                log_info("Certificate validated as DER format")
                return True
            except Exception as e:
                log_error(f"Invalid certificate format: {e}")
                return False

    def _load_trusted_certificates(self) -> bool:
        """
        Load trusted client certificates from configuration.

        Returns:
            bool: True if trusted certificates loaded successfully
        """
        try:
            self.trusted_certificates = []

            if not self.config.client_auth.trusted_certificates_pem:
                if not self.config.client_auth.trust_all_clients:
                    log_warn("Client authentication enabled but no trusted certificates configured")
                return True

            # Parse and validate each certificate
            for i, cert_pem in enumerate(self.config.client_auth.trusted_certificates_pem):
                try:
                    # Basic validation - check if it's a valid PEM certificate
                    cert_der = ssl.PEM_cert_to_DER_cert(cert_pem)
                    cert_hash = hashlib.sha256(cert_der).hexdigest()[:16]  # Short hash for logging

                    self.trusted_certificates.append(
                        {"pem": cert_pem, "der": cert_der, "hash": cert_hash}
                    )

                    log_info(f"Loaded trusted certificate {i + 1} (SHA256: {cert_hash})")

                except Exception as e:
                    log_error(f"Invalid trusted certificate {i + 1}: {e}")
                    return False

            log_info(f"Loaded {len(self.trusted_certificates)} trusted client certificates")
            return True

        except Exception as e:
            log_error(f"Failed to load trusted certificates: {e}")
            return False

    def validate_client_certificate(self, client_cert_pem: str) -> bool:
        """
        Validate if a client certificate is in the trust list.

        Args:
            client_cert_pem: Client certificate in PEM format

        Returns:
            bool: True if client certificate is trusted
        """
        if not self.config.client_auth.enabled:
            return True  # No authentication required

        if self.config.client_auth.trust_all_clients:
            return True  # Trust all clients

        if not self.trusted_certificates:
            log_warn("Client authentication enabled but no trusted certificates loaded")
            return False

        try:
            # Convert client certificate to DER for comparison
            client_cert_der = ssl.PEM_cert_to_DER_cert(client_cert_pem)
            client_hash = hashlib.sha256(client_cert_der).hexdigest()[:16]

            # Check if client certificate matches any trusted certificate
            for trusted_cert in self.trusted_certificates:
                if trusted_cert["der"] == client_cert_der:
                    log_info(f"Client certificate trusted (SHA256: {client_hash})")
                    return True

            log_error(f"Client certificate not trusted (SHA256: {client_hash})")
            return False

        except Exception as e:
            log_error(f"Error validating client certificate: {e}")
            return False

    def get_security_settings(
        self,
    ) -> Tuple[Optional[object], int, Optional[bytes], Optional[bytes]]:
        """
        Get security settings for opcua-asyncio server.

        Returns:
            Tuple of (security_policy_class, security_mode, certificate_data, private_key_data)
        """
        return (
            self.security_policy,
            self.security_mode,
            self.certificate_data,
            self.private_key_data,
        )

    async def generate_server_certificate(
        self,
        cert_path: str,
        key_path: str,
        common_name: str = "OpenPLC OPC-UA Server",
        key_size: int = 2048,
        valid_days: int = 3650,
        app_uri: str = None,
    ) -> bool:
        """
        Generate a self-signed certificate for the server with proper SAN extensions.

        This method auto-detects local IP addresses and includes them in the
        certificate's Subject Alternative Names (SANs) to prevent hostname
        validation errors when connecting via IP address.

        Args:
            cert_path: Path where certificate will be saved
            key_path: Path where private key will be saved
            common_name: Common name for the certificate
            key_size: RSA key size
            valid_days: Certificate validity period
            app_uri: Application URI for the certificate (from config)

        Returns:
            bool: True if certificate generated successfully
        """
        try:
            # Get system hostname for proper certificate validation
            system_hostname = socket.gethostname()

            # Extract hostname from endpoint if available
            endpoint_hostname = "localhost"  # default
            if hasattr(self.config, "endpoint") and self.config.endpoint:
                try:
                    # Convert opc.tcp:// to http:// for parsing
                    endpoint_url = self.config.endpoint.replace("opc.tcp://", "http://")
                    parsed = urlparse(endpoint_url)
                    if parsed.hostname and parsed.hostname != "0.0.0.0":
                        endpoint_hostname = parsed.hostname
                except Exception as e:
                    log_warn(f"Could not parse endpoint hostname: {e}")

            # Use provided app_uri or fallback to default
            if not app_uri:
                app_uri = "urn:autonomy-logic:openplc:opcua:server"

            # Collect all possible hostnames for SAN DNS entries
            dns_names = []
            # Add system hostname
            if system_hostname and system_hostname != "localhost":
                dns_names.append(system_hostname)
            # Add endpoint hostname if different
            if endpoint_hostname and endpoint_hostname not in dns_names:
                dns_names.append(endpoint_hostname)
            # Always include localhost
            if "localhost" not in dns_names:
                dns_names.append("localhost")

            # Auto-detect all local IP addresses for SAN
            local_ips = get_local_ip_addresses()
            ip_addresses = list(local_ips)

            log_info(f"Generating certificate with DNS SANs: {dns_names}")
            log_info(f"Generating certificate with IP SANs: {ip_addresses}")
            log_info(f"Application URI: {app_uri}")

            # Use custom certificate generation with multiple SANs
            success = generate_certificate_with_sans(
                cert_path=Path(cert_path),
                key_path=Path(key_path),
                app_uri=app_uri,
                dns_names=dns_names,
                ip_addresses=ip_addresses,
                common_name=common_name,
                key_size=key_size,
                valid_days=valid_days,
            )

            if success:
                log_info(f"Server certificate generated with proper SANs: {cert_path}")
            return success

        except Exception as e:
            log_error(f"Failed to generate server certificate: {e}")
            return False

    async def setup_server_security(self, server, security_profiles, app_uri: str = None) -> None:
        """Setup security policies and certificates for asyncua Server.

        Args:
            server: asyncua Server instance
            security_profiles: List of security profiles from config
            app_uri: Application URI for the certificate (from config)
        """
        # Setup security policies
        security_policies = []

        for profile in security_profiles:
            if not profile.enabled:
                continue

            policy_key = (profile.security_policy, profile.security_mode)
            policy_type = self.POLICY_TYPE_MAPPING.get(policy_key)

            if policy_type is not None:
                security_policies.append(policy_type)
                log_info(
                    f"Added security profile '{profile.name}': {profile.security_policy}/{profile.security_mode} -> {policy_type}"
                )
            else:
                log_warn(
                    f"Unsupported security policy/mode combination '{profile.security_policy}/{profile.security_mode}' for profile '{profile.name}', skipping"
                )

        # Create custom permission ruleset that allows ModifySubscription for users
        permission_ruleset = OpenPLCRoleRuleset()

        if security_policies:
            log_info("=== SECURITY MANAGER DEBUG ===")
            log_info(f"Setting {len(security_policies)} security policies: {security_policies}")
            server.set_security_policy(security_policies, permission_ruleset=permission_ruleset)
            log_info("Security policies applied to server successfully")
            log_info("Using OpenPLCRoleRuleset for subscription permission support")
            log_info("=== END SECURITY MANAGER DEBUG ===")
        else:
            # Default to no security if no profiles enabled
            log_warn("No security profiles enabled, defaulting to NoSecurity")
            server.set_security_policy(
                [ua.SecurityPolicyType.NoSecurity], permission_ruleset=permission_ruleset
            )

        # Setup server certificates if needed
        log_info("=== CERTIFICATE SETUP DEBUG ===")
        await self._setup_server_certificates_for_asyncua(server, app_uri)
        log_info("=== END CERTIFICATE SETUP DEBUG ===")

    async def _setup_server_certificates_for_asyncua(self, server, app_uri: str = None) -> None:
        """Setup server certificates for asyncua Server.

        Args:
            server: asyncua Server instance
            app_uri: Application URI for the certificate (from config)
        """
        if (
            hasattr(self.config, "security")
            and self.config.security.server_certificate_strategy == "auto_self_signed"
        ):
            # Generate self-signed certificate in persistent directory
            cert_dir = Path(self.plugin_dir) / "certs"
            cert_dir.mkdir(parents=True, exist_ok=True)

            key_file = cert_dir / "server_key.pem"
            cert_file = cert_dir / "server_cert.pem"

            hostname = socket.gethostname()
            # Use provided app_uri or fallback to config value
            if not app_uri:
                app_uri = getattr(
                    self.config.server, "application_uri", "urn:autonomy-logic:openplc:opcua:server"
                )

            # Check if we need to generate new certificates
            need_generation = False
            if not cert_file.exists() or not key_file.exists():
                log_info("Certificate files not found, will generate new ones")
                need_generation = True
            elif not self._is_certificate_valid(str(cert_file)):
                log_info("Certificate is expired or invalid, will regenerate")
                self._remove_certificate_files(str(cert_file), str(key_file))
                need_generation = True

            if need_generation:
                log_info(f"Generating new self-signed certificate in {cert_dir}")
                log_info(f"Certificate will be created for app_uri: {app_uri}")
                log_info(f"Certificate will be created for hostname: {hostname}")

                # Collect DNS names for SAN
                dns_names = [hostname]
                if hostname != "localhost":
                    dns_names.append("localhost")

                # Auto-detect all local IP addresses for SAN
                local_ips = get_local_ip_addresses()
                ip_addresses = list(local_ips)

                log_info(f"Certificate DNS SANs: {dns_names}")
                log_info(f"Certificate IP SANs: {ip_addresses}")

                # Use custom certificate generation with multiple SANs
                success = generate_certificate_with_sans(
                    cert_path=cert_file,
                    key_path=key_file,
                    app_uri=app_uri,
                    dns_names=dns_names,
                    ip_addresses=ip_addresses,
                    common_name="OpenPLC OPC-UA Server",
                )

                # Verify files were created
                if not success or not cert_file.exists() or not key_file.exists():
                    log_error(
                        f"Certificate files not created: cert={cert_file.exists()}, key={key_file.exists()}"
                    )
                    return

                log_info(f"Certificate files created successfully: {cert_file}, {key_file}")
            else:
                log_info(f"Using existing valid certificate files: {cert_file}, {key_file}")

            # Load and convert certificate from PEM to DER
            log_info(f"Loading server certificate from: {cert_file}")
            with open(cert_file, "rb") as f:
                cert_pem_data = f.read()
            log_info(f"Certificate PEM loaded: {len(cert_pem_data)} bytes")

            # Load private key
            log_info(f"Loading server private key from: {key_file}")
            with open(key_file, "rb") as f:
                key_pem_data = f.read()

            # Convert certificate and key from PEM to DER for asyncua compatibility
            from cryptography.hazmat.primitives.serialization import (
                load_pem_private_key,
            )

            try:
                # Convert certificate PEM to DER
                cert_obj = x509.load_pem_x509_certificate(cert_pem_data)
                cert_der_data = cert_obj.public_bytes(serialization.Encoding.DER)
                log_info(f"Certificate converted to DER: {len(cert_der_data)} bytes")

                # Convert private key PEM to DER
                private_key = load_pem_private_key(key_pem_data, password=None)
                key_der_data = private_key.private_bytes(
                    encoding=serialization.Encoding.DER,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
                log_info(f"Private key converted to DER: {len(key_der_data)} bytes")

                # Load certificate and key into server (both in DER format)
                log_info(f"Loading certificate into asyncua server: {len(cert_der_data)} bytes DER")
                await server.load_certificate(cert_der_data)
                log_info(f"Loading private key into asyncua server: {len(key_der_data)} bytes DER")
                await server.load_private_key(key_der_data)

            except Exception as e:
                log_error(f"Failed to load certificate/key into asyncua server: {e}")
                raise

            log_info("Self-signed server certificate loaded successfully into asyncua server")

        elif hasattr(self.config, "security") and self.config.security.server_certificate_custom:
            cert_path = self.config.security.server_certificate_custom
            key_path = self.config.security.server_private_key_custom
            if cert_path and key_path:
                try:
                    # Carregar certificado
                    with open(cert_path, "rb") as f:
                        cert_data = f.read()

                    # Carregar e converter chave privada de PEM para DER
                    with open(key_path, "rb") as f:
                        pem_key_data = f.read()

                    from cryptography.hazmat.primitives.serialization import (
                        load_pem_private_key,
                    )

                    private_key = load_pem_private_key(pem_key_data, password=None)
                    der_key_data = private_key.private_bytes(
                        encoding=serialization.Encoding.DER,
                        format=serialization.PrivateFormat.PKCS8,
                        encryption_algorithm=serialization.NoEncryption(),
                    )

                    await server.load_certificate(cert_data)
                    await server.load_private_key(der_key_data)
                    log_info("Custom server certificate loaded (PEM cert + DER key)")
                except Exception as e:
                    log_error(f"Failed to load custom certificate: {e}")

        elif self.certificate_data and self.private_key_data:
            await server.load_certificate(self.certificate_data)
            await server.load_private_key(self.private_key_data)
            log_info("SecurityManager certificates loaded into server")

    async def create_trust_store(self, trusted_certificates: List[str]) -> Optional[TrustStore]:
        """Create and configure TrustStore with trusted client certificates.

        Args:
            trusted_certificates: List of PEM certificate strings

        Returns:
            TrustStore instance or None if failed
        """
        if not trusted_certificates:
            return None

        try:
            # Create temporary directory for certificate files
            temp_dir = tempfile.mkdtemp(prefix="opcua_trust_")
            self._trust_store_temp_dir = temp_dir  # Store for cleanup
            cert_files = []

            for i, cert_pem in enumerate(trusted_certificates):
                try:
                    # Load and validate certificate using cryptography
                    cert = x509.load_pem_x509_certificate(cert_pem.encode())

                    # Convert to DER format and save to temporary file
                    cert_der = cert.public_bytes(encoding=serialization.Encoding.DER)

                    cert_file = os.path.join(temp_dir, f"trusted_cert_{i}.der")
                    with open(cert_file, "wb") as f:
                        f.write(cert_der)

                    cert_files.append(cert_file)
                    log_info(f"Added trusted certificate {i + 1} to trust store")

                except Exception as e:
                    log_warn(f"Failed to process trusted certificate {i + 1}: {e}")

            if cert_files:
                # Create TrustStore with certificate files
                trust_store = TrustStore(cert_files, [])
                await trust_store.load()
                log_info(f"TrustStore created with {len(cert_files)} certificates")
                return trust_store
            else:
                log_warn("No valid trusted certificates processed")
                return None

        except Exception as e:
            log_error(f"Failed to create TrustStore: {e}")
            return None

    def cleanup(self) -> None:
        """Clean up resources including temporary directories.

        Should be called when the server is shutting down.
        """
        if self._trust_store_temp_dir and os.path.exists(self._trust_store_temp_dir):
            try:
                shutil.rmtree(self._trust_store_temp_dir)
                log_info(f"Cleaned up trust store temp directory: {self._trust_store_temp_dir}")
                self._trust_store_temp_dir = None
            except Exception as e:
                log_warn(f"Failed to clean up trust store temp directory: {e}")

    async def setup_certificate_validation(self, server, trusted_certificates) -> None:
        """Setup certificate validation for asyncua Server.

        Args:
            server: asyncua Server instance
            trusted_certificates: List of certificate dictionaries with 'id' and 'pem' keys
        """
        if not trusted_certificates:
            return

        try:
            # Handle both List[str] and List[Dict[str, str]] formats
            cert_pems = []
            if trusted_certificates and isinstance(trusted_certificates[0], dict):
                # Extract PEM strings from certificate dictionaries
                cert_pems = [cert_info["pem"] for cert_info in trusted_certificates]
            else:
                # Already a list of PEM strings
                cert_pems = trusted_certificates

            # Create trust store
            trust_store = await self.create_trust_store(cert_pems)
            if not trust_store:
                log_error("Could not create trust store")
                return

            # Create certificate validator
            cert_validator = CertificateValidator(trust_store=trust_store)

            # Set validator on server
            server.set_certificate_validator(cert_validator)
            log_info("Certificate validation configured")

        except Exception as e:
            log_error(f"Failed to setup certificate validation: {e}")
