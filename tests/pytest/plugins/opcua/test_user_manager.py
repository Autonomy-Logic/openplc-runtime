"""
Unit tests for OPC-UA User Manager.

Tests cover:
- Password authentication success/failure
- Certificate authentication success/failure
- Anonymous authentication with profile restrictions
- Role mapping verification
- Rate limiting / brute-force protection
"""

import time
from dataclasses import dataclass
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest
from asyncua.crypto.permission_rules import User
from asyncua.server.user_managers import UserRole

# Import after path setup in conftest
from user_manager import (
    DEFAULT_ATTEMPT_WINDOW_SECONDS,
    DEFAULT_LOCKOUT_DURATION_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    OpenPLCUserManager,
    RateLimitConfig,
    RateLimiter,
)

# ============================================================================
# Mock Configuration Classes
# ============================================================================


@dataclass
class MockSecurityProfile:
    """Mock security profile for testing."""

    name: str
    enabled: bool
    security_policy: str
    security_mode: str
    auth_methods: List[str]


@dataclass
class MockUser:
    """Mock user configuration."""

    type: str
    username: Optional[str]
    password_hash: Optional[str]
    certificate_id: Optional[str]
    role: str


@dataclass
class MockServerConfig:
    """Mock server configuration."""

    security_profiles: List[MockSecurityProfile]


@dataclass
class MockSecurityConfig:
    """Mock security configuration."""

    trusted_client_certificates: List[dict]


@dataclass
class MockOpcuaConfig:
    """Mock OpcuaConfig for testing."""

    server: MockServerConfig
    security: MockSecurityConfig
    users: List[MockUser]


def create_test_config(
    users: Optional[List[MockUser]] = None,
    profiles: Optional[List[MockSecurityProfile]] = None,
    trusted_certs: Optional[List[dict]] = None,
) -> MockOpcuaConfig:
    """Create a mock config for testing."""
    if users is None:
        users = []
    if profiles is None:
        profiles = [
            MockSecurityProfile(
                name="insecure",
                enabled=True,
                security_policy="None",
                security_mode="None",
                auth_methods=["Username", "Anonymous"],
            )
        ]
    if trusted_certs is None:
        trusted_certs = []

    return MockOpcuaConfig(
        server=MockServerConfig(security_profiles=profiles),
        security=MockSecurityConfig(trusted_client_certificates=trusted_certs),
        users=users,
    )


# ============================================================================
# Rate Limiter Tests
# ============================================================================


class TestRateLimiter:
    """Tests for the RateLimiter class."""

    def test_init_default_config(self):
        """Test RateLimiter initializes with default config."""
        limiter = RateLimiter()
        assert limiter.config.max_attempts == DEFAULT_MAX_ATTEMPTS
        assert limiter.config.lockout_duration_seconds == DEFAULT_LOCKOUT_DURATION_SECONDS
        assert limiter.config.attempt_window_seconds == DEFAULT_ATTEMPT_WINDOW_SECONDS

    def test_init_custom_config(self):
        """Test RateLimiter initializes with custom config."""
        config = RateLimitConfig(
            max_attempts=3, lockout_duration_seconds=60, attempt_window_seconds=30
        )
        limiter = RateLimiter(config)
        assert limiter.config.max_attempts == 3
        assert limiter.config.lockout_duration_seconds == 60
        assert limiter.config.attempt_window_seconds == 30

    def test_not_locked_out_initially(self):
        """Test that new identifiers are not locked out."""
        limiter = RateLimiter()
        assert limiter.is_locked_out("user:test") is False

    def test_lockout_after_max_attempts(self):
        """Test lockout is triggered after max failed attempts."""
        config = RateLimitConfig(max_attempts=3, lockout_duration_seconds=60)
        limiter = RateLimiter(config)

        identifier = "user:attacker"

        # Record failed attempts up to max
        for i in range(3):
            assert limiter.is_locked_out(identifier) is False
            limiter.record_attempt(identifier, success=False)

        # Should now be locked out
        assert limiter.is_locked_out(identifier) is True

    def test_successful_auth_resets_tracker(self):
        """Test that successful auth resets the attempt counter."""
        config = RateLimitConfig(max_attempts=3)
        limiter = RateLimiter(config)

        identifier = "user:test"

        # Record some failed attempts
        limiter.record_attempt(identifier, success=False)
        limiter.record_attempt(identifier, success=False)

        # Successful auth
        limiter.record_attempt(identifier, success=True)

        # Should not be locked out
        assert limiter.is_locked_out(identifier) is False

        # Should be able to fail again without immediate lockout
        limiter.record_attempt(identifier, success=False)
        assert limiter.is_locked_out(identifier) is False

    def test_lockout_remaining_time(self):
        """Test getting remaining lockout time."""
        config = RateLimitConfig(max_attempts=1, lockout_duration_seconds=10)
        limiter = RateLimiter(config)

        identifier = "user:test"

        # Not locked out initially
        assert limiter.get_lockout_remaining(identifier) == 0.0

        # Trigger lockout
        limiter.record_attempt(identifier, success=False)

        # Should have remaining time
        remaining = limiter.get_lockout_remaining(identifier)
        assert 9 <= remaining <= 10

    def test_lockout_expires(self):
        """Test that lockout expires after duration."""
        config = RateLimitConfig(
            max_attempts=1,
            lockout_duration_seconds=0.1,  # 100ms for fast test
        )
        limiter = RateLimiter(config)

        identifier = "user:test"

        # Trigger lockout
        limiter.record_attempt(identifier, success=False)
        assert limiter.is_locked_out(identifier) is True

        # Wait for lockout to expire
        time.sleep(0.15)

        # Should no longer be locked out
        assert limiter.is_locked_out(identifier) is False

    def test_attempt_window_reset(self):
        """Test that attempt count resets after window expires."""
        config = RateLimitConfig(max_attempts=3, attempt_window_seconds=0.1)  # 100ms window
        limiter = RateLimiter(config)

        identifier = "user:test"

        # Record 2 failed attempts
        limiter.record_attempt(identifier, success=False)
        limiter.record_attempt(identifier, success=False)

        # Wait for window to expire
        time.sleep(0.15)

        # Record more attempts - should reset count
        limiter.record_attempt(identifier, success=False)
        limiter.record_attempt(identifier, success=False)

        # Should not be locked out (only 2 attempts in new window)
        assert limiter.is_locked_out(identifier) is False

    def test_cleanup_expired(self):
        """Test cleanup of expired trackers."""
        config = RateLimitConfig(
            max_attempts=1, lockout_duration_seconds=0.05, attempt_window_seconds=0.05
        )
        limiter = RateLimiter(config)

        # Create some trackers
        limiter.record_attempt("user:a", success=False)
        limiter.record_attempt("user:b", success=False)

        # Wait for expiration
        time.sleep(0.1)

        # Cleanup should remove expired entries
        removed = limiter.cleanup_expired()
        assert removed == 2
        assert len(limiter._trackers) == 0


# ============================================================================
# OpenPLCUserManager Tests
# ============================================================================


class TestOpenPLCUserManager:
    """Tests for the OpenPLCUserManager class."""

    def test_init_with_password_users(self):
        """Test initialization with password users."""
        users = [
            MockUser(
                type="password",
                username="operator",
                password_hash="$2b$10$hash",
                certificate_id=None,
                role="operator",
            ),
            MockUser(
                type="password",
                username="engineer",
                password_hash="$2b$10$hash2",
                certificate_id=None,
                role="engineer",
            ),
        ]
        config = create_test_config(users=users)

        manager = OpenPLCUserManager(config)

        assert len(manager.users) == 2
        assert "operator" in manager.users
        assert "engineer" in manager.users
        assert manager._user_roles["operator"] == "operator"
        assert manager._user_roles["engineer"] == "engineer"

    def test_init_with_certificate_users(self):
        """Test initialization with certificate users."""
        users = [
            MockUser(
                type="certificate",
                username=None,
                password_hash=None,
                certificate_id="cert1",
                role="engineer",
            ),
        ]
        config = create_test_config(users=users)

        manager = OpenPLCUserManager(config)

        assert len(manager.cert_users) == 1
        assert "cert1" in manager.cert_users
        assert manager._user_roles["cert:cert1"] == "engineer"

    def test_init_with_rate_limit_config(self):
        """Test initialization with custom rate limit config."""
        config = create_test_config()
        rate_config = RateLimitConfig(max_attempts=10)

        manager = OpenPLCUserManager(config, rate_limit_config=rate_config)

        assert manager.rate_limiter.config.max_attempts == 10


class TestPasswordAuthentication:
    """Tests for password authentication."""

    @pytest.fixture
    def manager_with_user(self):
        """Create manager with a test user."""
        # BCrypt hash for "testpass123"
        password_hash = "$2b$10$rJrPxLxGxNzVzqZqxZqZqO1234567890123456789012345678901234"

        users = [
            MockUser(
                type="password",
                username="testuser",
                password_hash=password_hash,
                certificate_id=None,
                role="operator",
            ),
        ]
        profiles = [
            MockSecurityProfile(
                name="test",
                enabled=True,
                security_policy="None",
                security_mode="None",
                auth_methods=["Username"],
            )
        ]
        config = create_test_config(users=users, profiles=profiles)
        return OpenPLCUserManager(config)

    def test_auth_fails_user_not_found(self, manager_with_user):
        """Test authentication fails for unknown user."""
        user = manager_with_user.get_user(None, username="unknown", password="password")
        assert user is None

    def test_auth_fails_wrong_password(self, manager_with_user):
        """Test authentication fails for wrong password."""
        with patch("user_manager.bcrypt") as mock_bcrypt:
            mock_bcrypt.checkpw.return_value = False

            user = manager_with_user.get_user(None, username="testuser", password="wrongpass")
            assert user is None

    def test_auth_success_returns_user_object(self, manager_with_user):
        """Test successful authentication returns asyncua User object."""
        with patch("user_manager.bcrypt") as mock_bcrypt:
            mock_bcrypt.checkpw.return_value = True

            user = manager_with_user.get_user(None, username="testuser", password="testpass123")

            assert user is not None
            assert isinstance(user, User)
            assert user.name == "testuser"
            assert user.role == UserRole.User  # operator maps to User
            assert user.openplc_role == "operator"

    def test_engineer_role_maps_to_admin(self):
        """Test engineer role maps to UserRole.Admin."""
        password_hash = "$2b$10$test"

        users = [
            MockUser(
                type="password",
                username="admin",
                password_hash=password_hash,
                certificate_id=None,
                role="engineer",
            ),
        ]
        profiles = [
            MockSecurityProfile(
                name="test",
                enabled=True,
                security_policy="None",
                security_mode="None",
                auth_methods=["Username"],
            )
        ]
        config = create_test_config(users=users, profiles=profiles)
        manager = OpenPLCUserManager(config)

        with patch("user_manager.bcrypt") as mock_bcrypt:
            mock_bcrypt.checkpw.return_value = True

            user = manager.get_user(None, username="admin", password="pass")

            assert user is not None
            assert user.role == UserRole.Admin
            assert user.openplc_role == "engineer"


class TestAnonymousAuthentication:
    """Tests for anonymous authentication."""

    def test_anonymous_allowed_when_profile_supports(self):
        """Test anonymous auth succeeds when profile allows it."""
        profiles = [
            MockSecurityProfile(
                name="insecure",
                enabled=True,
                security_policy="None",
                security_mode="None",
                auth_methods=["Anonymous"],
            )
        ]
        config = create_test_config(profiles=profiles)
        manager = OpenPLCUserManager(config)

        user = manager.get_user(None)  # No credentials = anonymous

        assert user is not None
        assert isinstance(user, User)
        assert user.name == "anonymous"
        assert user.role == UserRole.User
        assert user.openplc_role == "viewer"

    def test_anonymous_denied_when_profile_disallows(self):
        """Test anonymous auth fails when profile doesn't allow it."""
        profiles = [
            MockSecurityProfile(
                name="secure",
                enabled=True,
                security_policy="Basic256Sha256",
                security_mode="SignAndEncrypt",
                auth_methods=["Username", "Certificate"],  # No Anonymous
            )
        ]
        config = create_test_config(profiles=profiles)
        manager = OpenPLCUserManager(config)

        user = manager.get_user(None)  # No credentials = anonymous

        assert user is None


class TestRateLimitingIntegration:
    """Tests for rate limiting in authentication."""

    def test_lockout_blocks_authentication(self):
        """Test that locked out users cannot authenticate."""
        password_hash = "$2b$10$test"

        users = [
            MockUser(
                type="password",
                username="testuser",
                password_hash=password_hash,
                certificate_id=None,
                role="operator",
            ),
        ]
        profiles = [
            MockSecurityProfile(
                name="test",
                enabled=True,
                security_policy="None",
                security_mode="None",
                auth_methods=["Username"],
            )
        ]
        config = create_test_config(users=users, profiles=profiles)

        # Use low max_attempts for testing
        rate_config = RateLimitConfig(max_attempts=2, lockout_duration_seconds=60)
        manager = OpenPLCUserManager(config, rate_limit_config=rate_config)

        with patch("user_manager.bcrypt") as mock_bcrypt:
            mock_bcrypt.checkpw.return_value = False

            # First two attempts should fail but not lock out
            manager.get_user(None, username="testuser", password="wrong")
            result = manager.get_user(None, username="testuser", password="wrong")
            assert result is None

            # Third attempt should be blocked by rate limiting
            mock_bcrypt.checkpw.return_value = True  # Even correct password
            result = manager.get_user(None, username="testuser", password="correct")
            assert result is None  # Blocked by lockout

    def test_successful_auth_resets_rate_limit(self):
        """Test successful authentication resets rate limit counter."""
        password_hash = "$2b$10$test"

        users = [
            MockUser(
                type="password",
                username="testuser",
                password_hash=password_hash,
                certificate_id=None,
                role="operator",
            ),
        ]
        profiles = [
            MockSecurityProfile(
                name="test",
                enabled=True,
                security_policy="None",
                security_mode="None",
                auth_methods=["Username"],
            )
        ]
        config = create_test_config(users=users, profiles=profiles)

        rate_config = RateLimitConfig(max_attempts=3)
        manager = OpenPLCUserManager(config, rate_limit_config=rate_config)

        with patch("user_manager.bcrypt") as mock_bcrypt:
            # Two failed attempts
            mock_bcrypt.checkpw.return_value = False
            manager.get_user(None, username="testuser", password="wrong")
            manager.get_user(None, username="testuser", password="wrong")

            # Successful auth
            mock_bcrypt.checkpw.return_value = True
            result = manager.get_user(None, username="testuser", password="correct")
            assert result is not None

            # Should be able to fail again without lockout
            mock_bcrypt.checkpw.return_value = False
            manager.get_user(None, username="testuser", password="wrong")
            manager.get_user(None, username="testuser", password="wrong")

            # Still not locked out (counter was reset)
            mock_bcrypt.checkpw.return_value = True
            result = manager.get_user(None, username="testuser", password="correct")
            assert result is not None


class TestAuthMethodDetection:
    """Tests for authentication method detection."""

    def test_detect_username_method(self):
        """Test detection of username/password method."""
        config = create_test_config()
        manager = OpenPLCUserManager(config)

        method = manager._detect_auth_method("user", "pass", None)
        assert method == "Username"

    def test_detect_certificate_method(self):
        """Test detection of certificate method."""
        config = create_test_config()
        manager = OpenPLCUserManager(config)

        mock_cert = MagicMock()
        method = manager._detect_auth_method(None, None, mock_cert)
        assert method == "Certificate"

    def test_detect_anonymous_method(self):
        """Test detection of anonymous method."""
        config = create_test_config()
        manager = OpenPLCUserManager(config)

        method = manager._detect_auth_method(None, None, None)
        assert method == "Anonymous"

    def test_username_takes_precedence_over_certificate(self):
        """Test that username/password takes precedence over certificate."""
        config = create_test_config()
        manager = OpenPLCUserManager(config)

        mock_cert = MagicMock()
        method = manager._detect_auth_method("user", "pass", mock_cert)
        assert method == "Username"


class TestRoleMappings:
    """Tests for role mapping."""

    def test_viewer_maps_to_user(self):
        """Test viewer role maps to UserRole.User."""
        assert OpenPLCUserManager.ROLE_MAPPING["viewer"] == UserRole.User

    def test_operator_maps_to_user(self):
        """Test operator role maps to UserRole.User."""
        assert OpenPLCUserManager.ROLE_MAPPING["operator"] == UserRole.User

    def test_engineer_maps_to_admin(self):
        """Test engineer role maps to UserRole.Admin."""
        assert OpenPLCUserManager.ROLE_MAPPING["engineer"] == UserRole.Admin


class TestProfileMatching:
    """Tests for security profile matching."""

    def test_finds_enabled_profile(self):
        """Test finding an enabled profile that supports auth method."""
        profiles = [
            MockSecurityProfile(
                name="disabled",
                enabled=False,
                security_policy="None",
                security_mode="None",
                auth_methods=["Username"],
            ),
            MockSecurityProfile(
                name="enabled",
                enabled=True,
                security_policy="None",
                security_mode="None",
                auth_methods=["Username"],
            ),
        ]
        config = create_test_config(profiles=profiles)
        manager = OpenPLCUserManager(config)

        profile = manager._find_profile_by_auth_method("Username")

        assert profile is not None
        assert profile.name == "enabled"

    def test_returns_none_when_no_matching_profile(self):
        """Test returns None when no profile supports auth method."""
        profiles = [
            MockSecurityProfile(
                name="cert_only",
                enabled=True,
                security_policy="Basic256Sha256",
                security_mode="SignAndEncrypt",
                auth_methods=["Certificate"],
            ),
        ]
        config = create_test_config(profiles=profiles)
        manager = OpenPLCUserManager(config)

        profile = manager._find_profile_by_auth_method("Username")

        assert profile is None


class TestRateLimitIdentifier:
    """Tests for rate limit identifier generation."""

    def test_identifier_for_username(self):
        """Test identifier generation for username auth."""
        config = create_test_config()
        manager = OpenPLCUserManager(config)

        identifier = manager._get_rate_limit_identifier("testuser", None)
        assert identifier == "user:testuser"

    def test_identifier_for_certificate(self):
        """Test identifier generation for certificate auth."""
        config = create_test_config()
        manager = OpenPLCUserManager(config)

        # Mock certificate with bytes data
        mock_cert = b"certificate_data"

        with patch.object(manager, "_cert_to_fingerprint") as mock_fp:
            mock_fp.return_value = "AB:CD:EF:12:34:56:78:90:AB:CD:EF:12:34:56:78:90"

            identifier = manager._get_rate_limit_identifier(None, mock_cert)

            assert identifier is not None
            assert identifier.startswith("cert:")

    def test_identifier_none_for_anonymous(self):
        """Test no identifier for anonymous auth."""
        config = create_test_config()
        manager = OpenPLCUserManager(config)

        identifier = manager._get_rate_limit_identifier(None, None)
        assert identifier is None
