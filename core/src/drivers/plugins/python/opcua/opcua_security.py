"""
OPC-UA Security Utilities

This module provides utilities for handling OPC-UA security features including:
- Auto-generation of server certificates
- Certificate loading and validation
- Security policy and mode mapping
- Client trust list management
"""

import os
import sys
import ssl
import socket
import hashlib
import asyncio
import tempfile
import shutil
from pathlib import Path
from typing import Optional, Tuple, List
from urllib.parse import urlparse
from asyncua.crypto import uacrypto
from asyncua.crypto.cert_gen import setup_self_signed_certificate
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256, SecurityPolicyAes128Sha256RsaOaep, SecurityPolicyAes256Sha256RsaPss
from asyncua.crypto.truststore import TrustStore
from asyncua.crypto.validator import CertificateValidator
from asyncua.crypto.permission_rules import SimpleRoleRuleset, PermissionRuleset
from asyncua.server.user_managers import UserRole
from asyncua import ua
from cryptography.x509.oid import ExtensionOID, ExtendedKeyUsageOID
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

# Add directories to path for module access
_current_dir = os.path.dirname(os.path.abspath(__file__))
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

# Import logging (handle both package and direct loading)
try:
    from .opcua_logging import log_info, log_warn, log_error
except ImportError:
    from opcua_logging import log_info, log_warn, log_error


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
        "Aes256_Sha256_RsaPss": SecurityPolicyAes256Sha256RsaPss
    }

    # Mapping from config strings to opcua-asyncio message security modes
    SECURITY_MODE_MAPPING = {
        "None": 1,  # MessageSecurityMode.None
        "Sign": 2,  # MessageSecurityMode.Sign
        "SignAndEncrypt": 3  # MessageSecurityMode.SignAndEncrypt
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
        ("Aes128_Sha256_RsaOaep", "SignAndEncrypt"): ua.SecurityPolicyType.Aes128Sha256RsaOaep_SignAndEncrypt,
        ("Aes256_Sha256_RsaPss", "Sign"): ua.SecurityPolicyType.Aes256Sha256RsaPss_Sign,
        ("Aes256_Sha256_RsaPss", "SignAndEncrypt"): ua.SecurityPolicyType.Aes256Sha256RsaPss_SignAndEncrypt,
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

            log_info(f"Security initialized: policy={self.config.security_policy}, mode={self.config.security_mode}")
            return True

        except Exception as e:
            log_error(f"Failed to initialize security: {e}")
            return False

    async def _ensure_server_certificates(self) -> bool:
        """
        Ensure server certificates exist, generate if missing.

        Returns:
            bool: True if certificates are available
        """
        try:
            # Create certs directory if it doesn't exist
            os.makedirs(self.certs_dir, exist_ok=True)

            cert_path = os.path.join(self.certs_dir, self.SERVER_CERT_FILE)
            key_path = os.path.join(self.certs_dir, self.SERVER_KEY_FILE)

            # Check if certificates already exist
            if os.path.exists(cert_path) and os.path.exists(key_path):
                log_info(f"Found existing server certificates in {self.certs_dir}")
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
            with open(cert_path, 'rb') as cert_file:
                self.certificate_data = cert_file.read()

            # Load private key
            with open(key_path, 'rb') as key_file:
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
            ssl.PEM_cert_to_DER_cert(self.certificate_data.decode('utf-8'))
            
            # Enhanced validation using cryptography library
            try:
                from cryptography import x509
                from cryptography.hazmat.backends import default_backend
                import datetime
                
                cert = x509.load_pem_x509_certificate(self.certificate_data, default_backend())

                # Use timezone-aware datetime for comparison
                now_utc = datetime.datetime.now(datetime.timezone.utc)

                # Get certificate validity dates (prefer UTC versions if available)
                not_valid_after = getattr(cert, 'not_valid_after_utc', None)
                if not_valid_after is None:
                    not_valid_after = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)

                not_valid_before = getattr(cert, 'not_valid_before_utc', None)
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
                    san_ext = cert.extensions.get_extension_for_oid(x509.ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
                    san_names = san_ext.value
                    
                    # Log SAN entries for debugging
                    dns_names = [name.value for name in san_names if isinstance(name, x509.DNSName)]
                    ip_addresses = [name.value.compressed for name in san_names if isinstance(name, x509.IPAddress)]
                    uris = [name.value for name in san_names if isinstance(name, x509.UniformResourceIdentifier)]
                    
                    log_info(f"Certificate SAN DNS names: {dns_names}")
                    log_info(f"Certificate SAN IP addresses: {ip_addresses}")
                    log_info(f"Certificate SAN URIs: {uris}")
                    
                    # Check if we have expected entries
                    system_hostname = socket.gethostname()
                    if system_hostname not in dns_names and system_hostname != "localhost":
                        log_warn(f"System hostname '{system_hostname}' not found in certificate DNS SANs")
                    
                    # Check for application URI
                    expected_uri = "urn:autonomy-logic:openplc:opcua:server"
                    if expected_uri not in uris:
                        log_warn(f"Expected application URI '{expected_uri}' not found in certificate")
                    
                except x509.ExtensionNotFound:
                    log_warn("Certificate missing Subject Alternative Name extension")
                
                # Check key usage extensions
                try:
                    key_usage = cert.extensions.get_extension_for_oid(x509.ExtensionOID.KEY_USAGE).value
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

                    self.trusted_certificates.append({
                        'pem': cert_pem,
                        'der': cert_der,
                        'hash': cert_hash
                    })

                    log_info(f"Loaded trusted certificate {i+1} (SHA256: {cert_hash})")

                except Exception as e:
                    log_error(f"Invalid trusted certificate {i+1}: {e}")
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
                if trusted_cert['der'] == client_cert_der:
                    log_info(f"Client certificate trusted (SHA256: {client_hash})")
                    return True

            log_error(f"Client certificate not trusted (SHA256: {client_hash})")
            return False

        except Exception as e:
            log_error(f"Error validating client certificate: {e}")
            return False

    def get_security_settings(self) -> Tuple[Optional[object], int, Optional[bytes], Optional[bytes]]:
        """
        Get security settings for opcua-asyncio server.

        Returns:
            Tuple of (security_policy_class, security_mode, certificate_data, private_key_data)
        """
        return (
            self.security_policy,
            self.security_mode,
            self.certificate_data,
            self.private_key_data
        )

    async def generate_server_certificate(
        self,
        cert_path: str,
        key_path: str,
        common_name: str = "OpenPLC OPC-UA Server",
        key_size: int = 2048,
        valid_days: int = 365,
        app_uri: str = None
    ) -> bool:
        """
        Generate a self-signed certificate for the server with proper SAN extensions.

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
            if hasattr(self.config, 'endpoint') and self.config.endpoint:
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
            
            # IP addresses for SAN
            ip_addresses = ["127.0.0.1"]
            # Add 0.0.0.0 if endpoint uses it (for bind-all scenarios)
            if hasattr(self.config, 'endpoint') and "0.0.0.0" in self.config.endpoint:
                ip_addresses.append("0.0.0.0")
            
            log_info(f"Generating certificate with DNS SANs: {dns_names}")
            log_info(f"Generating certificate with IP SANs: {ip_addresses}")
            log_info(f"Application URI: {app_uri}")
            
            # Use the setup_self_signed_certificate function from asyncua with supported parameters
            await setup_self_signed_certificate(
                key_file=Path(key_path),
                cert_file=Path(cert_path),
                app_uri=app_uri,
                host_name=system_hostname,  # Use actual system hostname
                cert_use=[ExtendedKeyUsageOID.SERVER_AUTH],
                subject_attrs={
                    "countryName": "US",
                    "stateOrProvinceName": "CA",
                    "localityName": "California",
                    "organizationName": "Autonomy Logic",
                    "commonName": common_name
                },
            )

            log_info(f"Server certificate generated with proper SANs: {cert_path}")
            return True

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
                log_info(f"Added security profile '{profile.name}': {profile.security_policy}/{profile.security_mode} -> {policy_type}")
            else:
                log_warn(f"Unsupported security policy/mode combination '{profile.security_policy}/{profile.security_mode}' for profile '{profile.name}', skipping")
        
        # Create custom permission ruleset that allows ModifySubscription for users
        permission_ruleset = OpenPLCRoleRuleset()

        if security_policies:
            log_info(f"=== SECURITY MANAGER DEBUG ===")
            log_info(f"Setting {len(security_policies)} security policies: {security_policies}")
            server.set_security_policy(security_policies, permission_ruleset=permission_ruleset)
            log_info(f"Security policies applied to server successfully")
            log_info(f"Using OpenPLCRoleRuleset for subscription permission support")
            log_info(f"=== END SECURITY MANAGER DEBUG ===")
        else:
            # Default to no security if no profiles enabled
            log_warn("No security profiles enabled, defaulting to NoSecurity")
            server.set_security_policy([ua.SecurityPolicyType.NoSecurity], permission_ruleset=permission_ruleset)
        
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
        if hasattr(self.config, 'security') and self.config.security.server_certificate_strategy == "auto_self_signed":
            # Generate self-signed certificate in persistent directory
            cert_dir = Path(self.plugin_dir) / "certs"
            cert_dir.mkdir(parents=True, exist_ok=True)
            
            key_file = cert_dir / "server_key.pem"
            cert_file = cert_dir / "server_cert.pem"
            
            hostname = socket.gethostname()
            # Use provided app_uri or fallback to config value
            if not app_uri:
                app_uri = getattr(self.config.server, 'application_uri',
                                  'urn:autonomy-logic:openplc:opcua:server')
            
            # Only generate if files don't exist
            if not cert_file.exists() or not key_file.exists():
                log_info(f"Generating new self-signed certificate in {cert_dir}")
                log_info(f"Certificate will be created for app_uri: {app_uri}")
                log_info(f"Certificate will be created for hostname: {hostname}")
                await setup_self_signed_certificate(
                    key_file=key_file,
                    cert_file=cert_file,
                    app_uri=app_uri,
                    host_name=hostname,
                    cert_use=[ExtendedKeyUsageOID.SERVER_AUTH],
                    subject_attrs={}
                )
                
                # Verify files were created
                if not cert_file.exists() or not key_file.exists():
                    log_error(f"Certificate files not created: cert={cert_file.exists()}, key={key_file.exists()}")
                    return
                
                log_info(f"Certificate files created successfully: {cert_file}, {key_file}")
            else:
                log_info(f"Using existing certificate files: {cert_file}, {key_file}")
            
            # Load certificate (PEM format works)
            log_info(f"Loading server certificate from: {cert_file}")
            with open(cert_file, 'rb') as f:
                cert_data = f.read()
            log_info(f"Certificate loaded: {len(cert_data)} bytes")
            
            # Load private key and convert PEM to DER (asyncua requires DER for keys)
            log_info(f"Loading server private key from: {key_file}")
            with open(key_file, 'rb') as f:
                pem_key_data = f.read()
            
            # Convert private key from PEM to DER for asyncua compatibility
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            try:
                private_key = load_pem_private_key(pem_key_data, password=None)
                der_key_data = private_key.private_bytes(
                    encoding=serialization.Encoding.DER,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption()
                )
                log_info(f"Certificate data loaded and converted: cert={len(cert_data)} bytes, key={len(der_key_data)} bytes DER")
                
                # Load certificate and converted key into server
                log_info(f"Loading certificate into asyncua server: {len(cert_data)} bytes")
                await server.load_certificate(cert_data)  # PEM cert works
                log_info(f"Loading private key into asyncua server: {len(der_key_data)} bytes (DER format)")
                await server.load_private_key(der_key_data)  # DER key required
                
            except Exception as e:
                log_error(f"Failed to convert private key from PEM to DER: {e}")
                raise
            
            log_info("Self-signed server certificate loaded successfully into asyncua server")
        
        elif hasattr(self.config, 'security') and self.config.security.server_certificate_custom:
            cert_path = self.config.security.server_certificate_custom
            key_path = self.config.security.server_private_key_custom
            if cert_path and key_path:
                try:
                    # Carregar certificado
                    with open(cert_path, 'rb') as f:
                        cert_data = f.read()
                    
                    # Carregar e converter chave privada de PEM para DER
                    with open(key_path, 'rb') as f:
                        pem_key_data = f.read()
                    
                    from cryptography.hazmat.primitives.serialization import load_pem_private_key
                    private_key = load_pem_private_key(pem_key_data, password=None)
                    der_key_data = private_key.private_bytes(
                        encoding=serialization.Encoding.DER,
                        format=serialization.PrivateFormat.PKCS8,
                        encryption_algorithm=serialization.NoEncryption()
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
                    cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())
                    
                    # Convert to DER format and save to temporary file
                    cert_der = cert.public_bytes(encoding=serialization.Encoding.DER)
                    
                    cert_file = os.path.join(temp_dir, f"trusted_cert_{i}.der")
                    with open(cert_file, 'wb') as f:
                        f.write(cert_der)
                    
                    cert_files.append(cert_file)
                    log_info(f"Added trusted certificate {i+1} to trust store")
                    
                except Exception as e:
                    log_warn(f"Failed to process trusted certificate {i+1}: {e}")
            
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
