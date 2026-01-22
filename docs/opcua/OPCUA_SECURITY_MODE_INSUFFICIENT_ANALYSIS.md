# OPC-UA BadSecurityModeInsufficient Error Analysis

**Date:** 2026-01-22
**Context:** Username/password authentication over insecure (unencrypted) endpoint

## Summary

When using only the insecure security profile with Username authentication, OPC-UA clients display the error:

> Error 'BadSecurityModeInsufficient' was returned during ActivateSession, press 'Ignore' to suppress the error and continue connecting. If you ignore the error it is possible that the password is being sent in clear text.

This is **NOT a bug** - it's a security feature defined in the OPC-UA specification.

---

## What's Happening

This error is a **security feature** defined in the OPC-UA specification (Part 4, Section 7.36).

**The error comes from the OPC-UA client** (like UAExpert), not the server. When you configure:
- Security Policy: `None`
- Security Mode: `None`
- Auth Method: `Username` (password authentication)

The client detects that it would send the password **in plain text** over the network and warns the user. This is intentional behavior to protect against accidentally exposing credentials.

---

## OPC-UA Security Architecture

OPC-UA has **two separate security layers**:

| Layer | Purpose | Description |
|-------|---------|-------------|
| **Channel Security** | Encrypts communication between client/server | Configured via `security_policy` and `security_mode` |
| **Token Security** | Can encrypt user credentials separately | Can have its own SecurityPolicyUri |

When both are "None", passwords travel unencrypted over the network.

### Security Policy Options

| Policy | Mode | Result |
|--------|------|--------|
| `None` | `None` | No encryption (plaintext) |
| `Basic256Sha256` | `Sign` | Messages are signed (integrity) |
| `Basic256Sha256` | `SignAndEncrypt` | Full encryption (confidentiality + integrity) |

---

## Configuration Options

### Option 1: Use Encrypted Security Profile (Recommended)

Keep the `SignAndEncrypt` profile enabled alongside the insecure one:

```json
"security_profiles": [
  {
    "name": "insecure",
    "enabled": true,
    "security_policy": "None",
    "security_mode": "None",
    "auth_methods": ["Anonymous"]
  },
  {
    "name": "SignAndEncrypt",
    "enabled": true,
    "security_policy": "Basic256Sha256",
    "security_mode": "SignAndEncrypt",
    "auth_methods": ["Username", "Certificate"]
  }
]
```

This configuration:
- Allows Anonymous access on the insecure endpoint
- Requires encryption for Username/password authentication
- Follows OPC-UA security best practices

### Option 2: Accept the Risk (Click "Ignore")

If you're on a trusted local network (like a lab environment), clicking "Ignore" in the OPC-UA client will:
- Send the password in plaintext
- Connection will work normally
- **Only use this in isolated/trusted networks**

**Warning:** This exposes credentials to network sniffing attacks.

### Option 3: Token-Level Encryption (Advanced)

OPC-UA allows the UserIdentityToken to have its own security policy, even when channel security is "None". This means you could theoretically:
- Use `None` for channel security (no message encryption)
- Use `Basic256Sha256` for token security (password is encrypted)

This requires additional configuration in asyncua and is not currently implemented.

---

## Recommendations

| Environment | Recommendation |
|-------------|----------------|
| **Production / Industrial** | Use Option 1 - require encryption for password auth |
| **Development / Testing** | Option 2 is acceptable on isolated networks |
| **Internet-facing** | Always use SignAndEncrypt with certificates |

---

## Technical Details

### Error Code

- **Status Code:** `BadSecurityModeInsufficient` (0x80E60000)
- **Meaning:** "The operation is not permitted over the current secure channel"

### Where the Check Occurs

The security check happens during the `ActivateSession` phase:
1. Client connects to server (OpenSecureChannel)
2. Client creates session (CreateSession)
3. Client activates session with credentials (ActivateSession) - **Error occurs here**

The client library checks if sending credentials over the current security mode is safe before transmitting.

### asyncua Behavior

The asyncua library includes logic to:
1. Warn when creating open endpoints alongside encrypted ones
2. Try to find an encrypting policy for password transmission
3. Log warnings when no encrypting policy is available

From `asyncua/server/server.py`:
```python
# try to avoid plaintext password, find first policy with encryption
# ...
# No encrypting policy available, password may get transferred in plaintext
```

---

## References

- [OPC UA Part 4: Services - 7.37 UserTokenPolicy](https://reference.opcfoundation.org/Core/Part4/v104/docs/7.37)
- [Server with Authentication (user/password) and Encryption - GitHub Discussion #934](https://github.com/FreeOpcUa/opcua-asyncio/discussions/934)
- [Server set User with Password - GitHub Discussion #1386](https://github.com/FreeOpcUa/opcua-asyncio/discussions/1386)
- [asyncua PyPI](https://pypi.org/project/asyncua/)
