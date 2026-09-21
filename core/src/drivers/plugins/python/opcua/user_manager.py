"""
OPC UA User Manager.

This module provides authentication and user management for the OPC-UA server.
It supports password authentication, certificate authentication, and anonymous access.
Includes brute-force protection with rate limiting.
"""

import base64
import hashlib
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from asyncua.crypto.permission_rules import User
from asyncua.server.user_managers import UserManager, UserRole

# Import bcrypt with fallback to PBKDF2 (Python stdlib)
try:
    import bcrypt

    _bcrypt_available = True
except ImportError:
    _bcrypt_available = False

# PBKDF2 configuration (used when bcrypt is unavailable, e.g., on MSYS2/Cygwin)
PBKDF2_ITERATIONS = 600000  # OWASP recommendation for SHA256
PBKDF2_HASH_NAME = "sha256"
PBKDF2_SALT_LENGTH = 16


def _pbkdf2_hash_password(password: str) -> str:
    """
    Hash a password using PBKDF2-HMAC-SHA256.

    Format: pbkdf2:sha256:iterations$salt$hash
    where salt and hash are base64-encoded.

    Args:
        password: Plain text password

    Returns:
        PBKDF2 hash string
    """
    salt = os.urandom(PBKDF2_SALT_LENGTH)
    hash_bytes = hashlib.pbkdf2_hmac(
        PBKDF2_HASH_NAME, password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(hash_bytes).decode("ascii")
    return f"pbkdf2:{PBKDF2_HASH_NAME}:{PBKDF2_ITERATIONS}${salt_b64}${hash_b64}"


def _pbkdf2_verify_password(password: str, password_hash: str) -> bool:
    """
    Verify a password against a PBKDF2 hash.

    Args:
        password: Plain text password
        password_hash: PBKDF2 hash string (format: pbkdf2:sha256:iterations$salt$hash)

    Returns:
        True if password matches
    """
    try:
        # Parse the hash format: pbkdf2:sha256:iterations$salt$hash
        if not password_hash.startswith("pbkdf2:"):
            return False

        # Split into method part and data part
        method_part, data_part = password_hash.split("$", 1)
        # method_part = "pbkdf2:sha256:iterations"
        # data_part = "salt$hash"

        parts = method_part.split(":")
        if len(parts) != 3:
            return False

        _, hash_name, iterations_str = parts
        iterations = int(iterations_str)

        salt_b64, hash_b64 = data_part.split("$")
        salt = base64.b64decode(salt_b64)
        expected_hash = base64.b64decode(hash_b64)

        # Compute hash with same parameters
        computed_hash = hashlib.pbkdf2_hmac(
            hash_name, password.encode("utf-8"), salt, iterations
        )

        # Use constant-time comparison to prevent timing attacks
        import hmac

        return hmac.compare_digest(computed_hash, expected_hash)
    except Exception:
        return False


def hash_password(password: str) -> str:
    """
    Hash a password using the best available method.

    Uses bcrypt if available (Linux, macOS), falls back to PBKDF2 (MSYS2/Cygwin).

    Args:
        password: Plain text password

    Returns:
        Hashed password string
    """
    if _bcrypt_available:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    else:
        return _pbkdf2_hash_password(password)

# Add directories to path for module access
_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Import logging (handle both package and direct loading)
try:
    from .opcua_logging import log_debug, log_error, log_info, log_warn
except ImportError:
    from opcua_logging import log_debug, log_error, log_info, log_warn

from shared.plugin_config_decode.opcua_config_model import OpcuaConfig, normalize_role  # noqa: E402

# Rate limiting constants
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LOCKOUT_DURATION_SECONDS = 300  # 5 minutes
DEFAULT_ATTEMPT_WINDOW_SECONDS = 60  # 1 minute window for counting attempts


@dataclass
class AuthAttemptTracker:
    """Tracks authentication attempts for rate limiting."""

    attempts: int = 0
    first_attempt_time: float = 0.0
    lockout_until: float = 0.0


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    lockout_duration_seconds: float = DEFAULT_LOCKOUT_DURATION_SECONDS
    attempt_window_seconds: float = DEFAULT_ATTEMPT_WINDOW_SECONDS


class RateLimiter:
    """
    Rate limiter for authentication attempts.

    Tracks failed authentication attempts per identifier (username or IP)
    and enforces lockout after too many failures.
    """

    def __init__(self, config: Optional[RateLimitConfig] = None):
        """
        Initialize the rate limiter.

        Args:
            config: Rate limit configuration. Uses defaults if not provided.
        """
        self.config = config or RateLimitConfig()
        self._trackers: Dict[str, AuthAttemptTracker] = {}

    def is_locked_out(self, identifier: str) -> bool:
        """
        Check if an identifier is currently locked out.

        Args:
            identifier: The identifier to check (username, IP, etc.)

        Returns:
            True if locked out, False otherwise
        """
        tracker = self._trackers.get(identifier)
        if not tracker:
            return False

        current_time = time.time()

        # Check if lockout has expired
        if tracker.lockout_until > 0 and current_time < tracker.lockout_until:
            return True

        # Reset if lockout expired
        if tracker.lockout_until > 0 and current_time >= tracker.lockout_until:
            self._reset_tracker(identifier)
            return False

        return False

    def get_lockout_remaining(self, identifier: str) -> float:
        """
        Get remaining lockout time in seconds.

        Args:
            identifier: The identifier to check

        Returns:
            Remaining lockout time in seconds, or 0 if not locked out
        """
        tracker = self._trackers.get(identifier)
        if not tracker or tracker.lockout_until <= 0:
            return 0.0

        remaining = tracker.lockout_until - time.time()
        return max(0.0, remaining)

    def record_attempt(self, identifier: str, success: bool) -> None:
        """
        Record an authentication attempt.

        Args:
            identifier: The identifier (username, IP, etc.)
            success: Whether the attempt was successful
        """
        current_time = time.time()

        if success:
            # Reset on successful authentication
            self._reset_tracker(identifier)
            return

        # Get or create tracker
        if identifier not in self._trackers:
            self._trackers[identifier] = AuthAttemptTracker(
                attempts=0, first_attempt_time=current_time, lockout_until=0.0
            )

        tracker = self._trackers[identifier]

        # Reset attempt count if window has expired
        if current_time - tracker.first_attempt_time > self.config.attempt_window_seconds:
            tracker.attempts = 0
            tracker.first_attempt_time = current_time

        # Increment attempt count
        tracker.attempts += 1

        # Check if lockout threshold reached
        if tracker.attempts >= self.config.max_attempts:
            tracker.lockout_until = current_time + self.config.lockout_duration_seconds

    def _reset_tracker(self, identifier: str) -> None:
        """Reset the tracker for an identifier."""
        if identifier in self._trackers:
            del self._trackers[identifier]

    def cleanup_expired(self) -> int:
        """
        Clean up expired trackers to prevent memory growth.

        Returns:
            Number of trackers removed
        """
        current_time = time.time()
        expired = []

        for identifier, tracker in self._trackers.items():
            # Remove if lockout expired and no recent attempts
            if tracker.lockout_until > 0 and current_time >= tracker.lockout_until:
                expired.append(identifier)
            # Remove if attempt window expired and not locked out
            elif (
                tracker.lockout_until <= 0
                and current_time - tracker.first_attempt_time > self.config.attempt_window_seconds
            ):
                expired.append(identifier)

        for identifier in expired:
            del self._trackers[identifier]

        return len(expired)


class OpenPLCUserManager(UserManager):
    """
    Custom user manager for OpenPLC authentication.

    Supports:
    - Password authentication (bcrypt or PBKDF2 hashed)
    - Certificate authentication (fingerprint matching)
    - Anonymous access
    - Brute-force protection with rate limiting

    Password hashing:
    - bcrypt: Used on Linux/macOS where bcrypt is available
    - PBKDF2-HMAC-SHA256: Used on MSYS2/Cygwin where bcrypt cannot be built

    Maps OpenPLC roles to asyncua UserRole enum:
    - viewer -> UserRole.User (read-only)
    - operator -> UserRole.User (read/write via callbacks)
    - engineer -> UserRole.Admin (full access)

    Returns asyncua User objects for proper integration with the asyncua library.
    """

    # Map OpenPLC roles to asyncua UserRole enum
    ROLE_MAPPING = {
        "viewer": UserRole.User,  # Read-only access
        "operator": UserRole.User,  # Read/write access (controlled by callbacks)
        "engineer": UserRole.Admin,  # Full access
    }

    def __init__(self, config: OpcuaConfig, rate_limit_config: Optional[RateLimitConfig] = None):
        """
        Initialize the user manager.

        Args:
            config: OpcuaConfig instance with users and security profiles
            rate_limit_config: Optional rate limiting configuration.
                              Uses defaults if not provided.
        """
        super().__init__()
        self.config = config

        # Initialize rate limiter for brute-force protection
        self.rate_limiter = RateLimiter(rate_limit_config)

        # Build user dictionaries
        # A password user with no hash cannot authenticate — `_validate_password`
        # has no format to match and refuses it — so registering it only creates
        # an account that looks configured and never works. Refuse it at load,
        # where the reason can be said once, instead of once per failed login.
        _credentialled = []
        for user in config.users:
            if user.type != "password":
                continue
            if not getattr(user, "password_hash", None):
                log_error(
                    f"OPC-UA user '{user.username}' has no password hash and is REFUSED. "
                    f"Set a password for it in the editor, or remove it."
                )
                continue
            _credentialled.append(user)
        self.users = {user.username: user for user in _credentialled}
        self.cert_users = {
            user.certificate_id: user for user in config.users if user.type == "certificate"
        }

        # Store OpenPLC roles separately to avoid modifying config objects
        self._user_roles: Dict[str, str] = {}
        for user in config.users:
            if user.type == "password" and user.username:
                self._user_roles[user.username] = str(user.role)
            elif user.type == "certificate" and user.certificate_id:
                self._user_roles[f"cert:{user.certificate_id}"] = str(user.role)

        # Anonymous role is a PER-PROFILE field, but anonymous authentication
        # carries no endpoint identity into get_user(), so the lookup can only
        # take the FIRST enabled profile that offers Anonymous
        # (_find_profile_by_auth_method). The editor is where two Anonymous
        # profiles should be prevented; this is the belt-and-suspenders: if more
        # than one enabled profile offers Anonymous, warn at load (once, where
        # the admin sees it) that list order decides the role, and name the one
        # that wins. Behaviour is unchanged — the first profile is still used.
        anon_profiles = [
            p for p in getattr(config.server, "security_profiles", [])
            if getattr(p, "enabled", False) and "Anonymous" in getattr(p, "auth_methods", [])
        ]
        if len(anon_profiles) > 1:
            winner = anon_profiles[0]
            detail = ", ".join(
                f"'{getattr(p, 'name', '?')}'(anonymous_role={getattr(p, 'anonymous_role', 'viewer')})"
                for p in anon_profiles
            )
            log_warn(
                "OPC-UA: more than one enabled security profile offers Anonymous "
                f"[{detail}]. Anonymous sessions cannot be mapped to a specific "
                f"endpoint, so the FIRST profile in list order wins — "
                f"'{getattr(winner, 'name', '?')}' with anonymous_role "
                f"'{getattr(winner, 'anonymous_role', 'viewer')}'. Configure a single "
                "Anonymous profile in the editor to make this unambiguous."
            )

        log_info(
            f"UserManager initialized: {len(self.users)} password users, "
            f"{len(self.cert_users)} certificate users, rate limiting enabled"
        )

    def get_user(
        self,
        iserver,
        username: Optional[str] = None,
        password: Optional[str] = None,
        certificate: Optional[Any] = None,
    ) -> Optional[User]:
        """
        Authenticate user with security profile enforcement and rate limiting.

        Note: asyncua passes InternalServer as the first argument, not a session.
        The security policy URI is not available at this level, so we select
        the security profile based on the authentication method being used.

        Rate limiting is applied to prevent brute-force attacks. After too many
        failed attempts, the user/identifier is locked out for a configurable period.

        Args:
            iserver: The internal server object (passed by asyncua)
            username: Username for password authentication
            password: Password for password authentication
            certificate: Certificate for certificate authentication

        Returns:
            asyncua User object with role attribute, or None if authentication fails
        """
        # Detect authentication method from provided credentials
        auth_method = self._detect_auth_method(username, password, certificate)

        # Determine rate limit identifier based on auth method
        rate_limit_id = self._get_rate_limit_identifier(username, certificate)

        # Check rate limiting (skip for anonymous)
        if auth_method != "Anonymous" and rate_limit_id:
            if self.rate_limiter.is_locked_out(rate_limit_id):
                remaining = self.rate_limiter.get_lockout_remaining(rate_limit_id)
                log_warn(
                    f"Authentication blocked for '{rate_limit_id}': "
                    f"locked out for {remaining:.0f} more seconds"
                )
                return None

        log_debug(f"Authentication attempt: method={auth_method}")

        # Find a security profile that supports this authentication method
        profile = self._find_profile_by_auth_method(auth_method)

        if not profile:
            log_error(
                f"No security profile found that supports authentication method '{auth_method}'"
            )
            # Record failed attempt for rate limiting
            if rate_limit_id:
                self.rate_limiter.record_attempt(rate_limit_id, success=False)
            return None

        log_debug(f"Using security profile '{profile.name}' for {auth_method} authentication")

        # Authenticate based on method
        user = None
        openplc_role = None

        if auth_method == "Username" and username and password:
            user, openplc_role = self._authenticate_password(username, password)

        elif auth_method == "Certificate" and certificate:
            user, openplc_role = self._authenticate_certificate(certificate)

        elif auth_method == "Anonymous":
            user, openplc_role = self._authenticate_anonymous(profile)

        # Record attempt result for rate limiting (skip anonymous)
        if auth_method != "Anonymous" and rate_limit_id:
            self.rate_limiter.record_attempt(rate_limit_id, success=(user is not None))

        if user:
            # Store OpenPLC role as attribute for permission callbacks
            user.openplc_role = openplc_role
            log_debug(
                f"User '{user.name or 'anonymous'}' authenticated successfully "
                f"using '{auth_method}' method for profile '{profile.name}' "
                f"(role: {openplc_role})"
            )
            return user
        else:
            log_warn(
                f"Authentication failed for method '{auth_method}' on profile '{profile.name}'"
            )
            return None

    def _get_rate_limit_identifier(
        self, username: Optional[str], certificate: Optional[Any]
    ) -> Optional[str]:
        """
        Get identifier for rate limiting.

        Args:
            username: Username if provided
            certificate: Certificate if provided

        Returns:
            Identifier string for rate limiting, or None
        """
        if username:
            return f"user:{username}"
        elif certificate:
            fingerprint = self._cert_to_fingerprint(certificate)
            if fingerprint:
                return f"cert:{fingerprint[:32]}"  # Use first 32 chars of fingerprint
        return None

    def _authenticate_password(
        self, username: str, password: str
    ) -> tuple[Optional[User], Optional[str]]:
        """
        Authenticate using username and password.

        Args:
            username: The username
            password: The password

        Returns:
            Tuple of (asyncua User object, OpenPLC role string) or (None, None)
        """
        if username not in self.users:
            log_warn(f"User '{username}' not found in configuration")
            return None, None

        config_user = self.users[username]
        if not self._validate_password(password, config_user.password_hash):
            log_warn(f"Password validation failed for user '{username}'")
            return None, None

        # Get OpenPLC role and map to asyncua role
        openplc_role = self._user_roles.get(username, "viewer")
        asyncua_role = self.ROLE_MAPPING.get(openplc_role, UserRole.User)

        # Return asyncua User object
        return User(role=asyncua_role, name=username), openplc_role

    def _authenticate_certificate(self, certificate: Any) -> tuple[Optional[User], Optional[str]]:
        """
        Authenticate using certificate.

        Args:
            certificate: The client certificate

        Returns:
            Tuple of (asyncua User object, OpenPLC role string) or (None, None)
        """
        cert_id = self._extract_cert_id(certificate)
        if not cert_id or cert_id not in self.cert_users:
            log_warn(f"Certificate not found in trusted certificates (cert_id={cert_id})")
            return None, None

        # Get OpenPLC role and map to asyncua role
        openplc_role = self._user_roles.get(f"cert:{cert_id}", "viewer")
        asyncua_role = self.ROLE_MAPPING.get(openplc_role, UserRole.User)

        log_debug(f"Certificate authenticated as user with role '{openplc_role}'")

        # Return asyncua User object
        return User(role=asyncua_role, name=f"cert:{cert_id}"), openplc_role

    def _authenticate_anonymous(self, profile: Any) -> tuple[Optional[User], Optional[str]]:
        """
        Authenticate as anonymous user.

        Anonymous role assignment is EXPLICIT, taken from the profile's
        ``anonymous_role`` field, which the editor defaults to the
        least-privilege 'viewer' (read-only). An anonymous client carries no
        identity, so what it may do is a deliberate configuration choice rather
        than something inferred from whether users happen to exist. An
        administrator raises the role to 'operator' or 'engineer' only to allow
        unauthenticated writes.

        Backward compatibility: projects authored before this field carry no
        ``anonymous_role``; the config model defaults it to 'viewer', so an old
        project's anonymous sessions become read-only. (This is a deliberate
        security tightening from the previous "no users => engineer" heuristic;
        anyone who relied on anonymous writes sets the role to engineer.)

        Either way, per-variable permissions still apply. A variable whose
        viewer permission is "rw" is writable by anyone; one whose engineer
        permission is "r" is read-only even for the engineer role.

        Args:
            profile: The security profile

        Returns:
            Tuple of (asyncua User object, OpenPLC role string) or (None, None)
        """
        if "Anonymous" not in profile.auth_methods:
            log_warn("Anonymous authentication not allowed for this profile")
            return None, None

        # Explicit, config-driven role (defaults to viewer). The value is
        # validated at parse time (opcua_config_model.SecurityProfile), and
        # normalized here through normalize_role() — the same strip+lowercase
        # normalization callbacks applies to every other role — so casing or
        # whitespace ("Engineer", " engineer ") cannot make an anonymous session
        # silently degrade to viewer. Map to the asyncua role for
        # operation-level checks; per-variable enforcement uses the OpenPLC role
        # string. normalize_role always returns a ROLE_MAPPING key.
        openplc_role = normalize_role(getattr(profile, "anonymous_role", "viewer") or "viewer")
        asyncua_role = self.ROLE_MAPPING[openplc_role]

        log_debug(
            f"Anonymous session on profile '{getattr(profile, 'name', '?')}' "
            f"mapped to role '{openplc_role}'"
        )
        return User(role=asyncua_role, name="anonymous"), openplc_role

    def _extract_cert_id(self, certificate: Any) -> Optional[str]:
        """
        Extract certificate ID using fingerprint matching.

        Args:
            certificate: The client certificate

        Returns:
            Certificate ID if found in trusted list, None otherwise
        """
        try:
            # Convert session certificate to fingerprint
            client_fingerprint = self._cert_to_fingerprint(certificate)
            if not client_fingerprint:
                return None

            # Compare with configured certificate fingerprints
            for cert_info in self.config.security.trusted_client_certificates:
                config_fingerprint = self._pem_to_fingerprint(cert_info["pem"])
                if config_fingerprint and client_fingerprint == config_fingerprint:
                    log_debug(
                        f"Certificate matched: {cert_info['id']} "
                        f"(fingerprint: {client_fingerprint[:16]}...)"
                    )
                    return cert_info["id"]

            log_warn(
                f"Certificate not found in trusted list (fingerprint: {client_fingerprint[:16]}...)"
            )
        except Exception as e:
            log_error(f"Certificate fingerprint extraction failed: {e}")

        return None

    def _cert_to_fingerprint(self, certificate: Any) -> Optional[str]:
        """
        Convert certificate object to SHA256 fingerprint.

        Args:
            certificate: Certificate object (various formats supported)

        Returns:
            Fingerprint string (colon-separated hex) or None
        """
        try:
            if hasattr(certificate, "der"):
                # Certificate object with der attribute
                cert_der = certificate.der
            elif hasattr(certificate, "data"):
                # Certificate object with data attribute
                cert_der = certificate.data
            elif isinstance(certificate, bytes):
                # Raw certificate data
                cert_der = certificate
            else:
                # Try to convert to string and then decode
                cert_str = str(certificate)
                if "-----BEGIN CERTIFICATE-----" in cert_str:
                    # PEM format - extract base64 content
                    cert_lines = cert_str.split("\n")
                    cert_b64 = "".join(
                        [line for line in cert_lines if not line.startswith("-----")]
                    )
                    cert_der = base64.b64decode(cert_b64)
                else:
                    log_warn(f"Unknown certificate format: {type(certificate)}")
                    return None

            # Calculate SHA256 fingerprint
            fingerprint = hashlib.sha256(cert_der).hexdigest().upper()
            return ":".join(fingerprint[i : i + 2] for i in range(0, len(fingerprint), 2))
        except Exception as e:
            log_error(f"Failed to extract certificate fingerprint: {e}")
            return None

    def _pem_to_fingerprint(self, pem_str: str) -> Optional[str]:
        """
        Convert PEM certificate string to SHA256 fingerprint.

        Args:
            pem_str: PEM-encoded certificate string

        Returns:
            Fingerprint string (colon-separated hex) or None
        """
        try:
            # Extract base64 content from PEM
            pem_lines = pem_str.strip().split("\n")
            cert_b64 = "".join([line for line in pem_lines if not line.startswith("-----")])
            cert_der = base64.b64decode(cert_b64)

            # Calculate SHA256 fingerprint
            fingerprint = hashlib.sha256(cert_der).hexdigest().upper()
            return ":".join(fingerprint[i : i + 2] for i in range(0, len(fingerprint), 2))
        except Exception as e:
            log_error(f"Failed to convert PEM to fingerprint: {e}")
            return None

    def _detect_auth_method(
        self, username: Optional[str], password: Optional[str], certificate: Optional[Any]
    ) -> str:
        """
        Detect which authentication method is being used.

        Priority order:
        1. Username/Password - explicit user credentials take precedence
        2. Certificate - used when no credentials provided but cert present
        3. Anonymous - fallback when nothing provided

        Note: Certificate is always present on secure connections (TLS), so we must
        check for username/password first to avoid incorrectly detecting certificate
        auth when the user intended to use credentials.

        Args:
            username: Username if provided
            password: Password if provided
            certificate: Certificate if provided

        Returns:
            Authentication method: "Certificate", "Username", or "Anonymous"
        """
        if username and password:
            return "Username"
        elif certificate:
            return "Certificate"
        else:
            return "Anonymous"

    def _find_profile_by_auth_method(self, auth_method: str) -> Optional[Any]:
        """
        Find a security profile that supports the given authentication method.

        Args:
            auth_method: The authentication method to find

        Returns:
            Security profile object or None
        """
        for profile in self.config.server.security_profiles:
            if not profile.enabled:
                continue
            if auth_method in profile.auth_methods:
                log_debug(f"Found profile '{profile.name}' supporting {auth_method}")
                return profile

        log_warn(f"No enabled profile found supporting authentication method: {auth_method}")
        return None

    def _validate_password(self, password: str, password_hash: str) -> bool:
        """
        Validate password against hash using bcrypt or PBKDF2.

        Automatically detects the hash type:
        - bcrypt hashes start with $2a$, $2b$, or $2y$
        - PBKDF2 hashes start with pbkdf2:

        Args:
            password: Plain text password
            password_hash: Bcrypt or PBKDF2 hash

        Returns:
            True if password matches
        """
        # Detect hash type and validate accordingly
        if password_hash.startswith("pbkdf2:"):
            # PBKDF2 hash - use stdlib (works everywhere)
            return _pbkdf2_verify_password(password, password_hash)
        elif password_hash.startswith(("$2a$", "$2b$", "$2y$")):
            # bcrypt hash
            if _bcrypt_available:
                try:
                    return bcrypt.checkpw(password.encode(), password_hash.encode())
                except Exception as e:
                    log_error(f"bcrypt validation error: {e}")
                    return False
            else:
                log_error(
                    "bcrypt hash detected but bcrypt not available. "
                    "Re-hash password with PBKDF2 or install bcrypt."
                )
                return False
        else:
            log_error(f"Unknown password hash format: {password_hash[:10]}...")
            return False
