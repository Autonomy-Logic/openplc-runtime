# OPC-UA Plugin Authentication Implementation Report

**Date:** 2026-01-22
**Branch:** RTOP-100-OPC-UA
**asyncua Version:** 1.1.8

## Executive Summary

The OpenPLC OPC-UA plugin's username/password authentication implementation **is correctly aligned with asyncua 1.1.8 patterns**. The implementation follows the recommended approach from the asyncua library documentation and community examples.

---

## Comparison Table: OpenPLC vs asyncua 1.1.8

| Aspect | asyncua 1.1.8 Pattern | OpenPLC Implementation | Status |
|--------|----------------------|------------------------|--------|
| **UserManager Interface** | Extends `UserManager` base class | `OpenPLCUserManager(UserManager)` | Correct |
| **get_user signature** | `get_user(self, iserver, username=None, password=None, certificate=None)` | Exact same signature at `user_manager.py:88-94` | Correct |
| **Return value** | `User` object with `role` attribute, or `None` | Returns user object with `role` (UserRole enum) or `None` | Correct |
| **Server integration** | `Server(user_manager=UserManager())` | `Server(user_manager=self.user_manager)` at `server.py:188` | Correct |
| **UserRole enum** | `from asyncua.server.user_managers import UserRole` | Same import at `user_manager.py:15` | Correct |
| **Password storage** | No specific requirement | bcrypt hashes (industry standard) | Good |

---

## Detailed Analysis

### 1. UserManager Class Implementation (`user_manager.py`)

**Correct Implementation:**
```python
# Line 15: Correct import from asyncua
from asyncua.server.user_managers import UserManager, UserRole

# Line 41: Proper inheritance
class OpenPLCUserManager(UserManager):
    ...

# Lines 88-94: Correct method signature
def get_user(
    self,
    iserver,
    username: Optional[str] = None,
    password: Optional[str] = None,
    certificate: Optional[Any] = None
) -> Optional[Any]:
```

This matches the asyncua documentation exactly:
```python
# asyncua pattern:
class UserManager:
    def get_user(self, iserver, username=None, password=None, certificate=None):
        raise NotImplementedError
```

### 2. Server Integration (`server.py:188`)

**Correct Implementation:**
```python
# Line 188: Passes user_manager to Server constructor
self.server = Server(user_manager=self.user_manager)
```

This aligns with asyncua's recommended pattern:
```python
# asyncua documentation:
server = Server(user_manager=UserManager())
```

### 3. Role Mapping (`user_manager.py:56-61`)

**Implementation:**
```python
ROLE_MAPPING = {
    "viewer": UserRole.User,      # Read-only access
    "operator": UserRole.User,    # Read/write via callbacks
    "engineer": UserRole.Admin    # Full access
}
```

This is consistent with asyncua's `UserRole` enum which has `User` and `Admin` levels.

### 4. Password Validation (`user_manager.py:369-389`)

**Strengths:**
- Uses bcrypt for password hashing (industry standard)
- Fails securely if bcrypt is unavailable
- No plaintext password storage

**Implementation:**
```python
def _validate_password(self, password: str, password_hash: str) -> bool:
    if _bcrypt_available:
        try:
            return bcrypt.checkpw(password.encode(), password_hash.encode())
        except Exception as e:
            log_error(f"bcrypt validation error: {e}")
            return False
    else:
        log_error("bcrypt not available - password authentication disabled for security")
        return False
```

---

## Minor Observations (Not Issues)

| Item | Current State | asyncua Default | Impact |
|------|--------------|-----------------|--------|
| User return type | `SimpleNamespace` / config `User` | `User` from `asyncua.crypto.permission_rules` | Works correctly - asyncua only checks for `role` attribute |
| Anonymous users | `SimpleNamespace()` with `role` | `User(role=UserRole.User)` | Functionally equivalent |

The implementation returns user objects that have the required `role` attribute, which is all asyncua needs for authorization decisions.

---

## Test Coverage Gap

**Finding:** No unit tests exist for the `OpenPLCUserManager` class.

**Recommendation:** Consider adding tests for:
- Password authentication success/failure
- Certificate authentication success/failure
- Anonymous authentication with profile restrictions
- Role mapping verification

---

## Configuration Validation

The current config file (`opcua.json`) shows proper usage:

```json
{
  "users": [
    {
      "type": "certificate",
      "certificate_id": "engineer_cert",
      "role": "engineer"
    },
    {
      "type": "password",
      "username": "operator",
      "password_hash": "$2b$10$Y/WT4Z8ku9hObwSPk1bmY...",
      "role": "operator"
    }
  ]
}
```

---

## Conclusion

**The implementation is healthy and correctly follows asyncua 1.1.8 patterns.**

No changes are required for core functionality. The implementation:
1. Uses the correct `UserManager` interface
2. Has the correct `get_user()` signature
3. Integrates properly with asyncua `Server`
4. Uses appropriate security practices (bcrypt hashing)

---

## Optional Improvements (Not Required)

| Priority | Improvement | Rationale |
|----------|-------------|-----------|
| Low | Add unit tests for `OpenPLCUserManager` | Increase confidence in auth logic |
| Low | Return asyncua's `User` class directly | Closer adherence to asyncua patterns (not required for functionality) |
| Low | Add rate limiting on auth attempts | Security hardening against brute force |

---

## References

- [Server set User with Password - GitHub Discussion #1386](https://github.com/FreeOpcUa/opcua-asyncio/discussions/1386)
- [Server with Authentication (user/password) and Encryption - GitHub Discussion #934](https://github.com/FreeOpcUa/opcua-asyncio/discussions/934)
- [asyncua PyPI](https://pypi.org/project/asyncua/)
- [asyncua server.py](https://github.com/FreeOpcUa/opcua-asyncio/blob/master/asyncua/server/server.py)
- [asyncua server-with-encryption.py example](https://github.com/FreeOpcUa/opcua-asyncio/blob/master/examples/server-with-encryption.py)
