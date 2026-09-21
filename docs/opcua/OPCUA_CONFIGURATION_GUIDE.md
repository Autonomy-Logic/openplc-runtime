# OPC-UA Plugin Configuration Guide

This document explains each field in the `opcua.json` configuration file and how it influences the OPC-UA server behavior.

## Table of Contents

- [Overview](#overview)
- [Top-Level Structure](#top-level-structure)
- [Server Configuration](#server-configuration)
- [Security Configuration](#security-configuration)
- [Users Configuration](#users-configuration)
- [Cycle Time](#cycle-time)
- [Address Space](#address-space)
  - [Variables](#variables)
  - [Structures](#structures)
  - [Arrays](#arrays)
- [Data Types Reference](#data-types-reference)
- [Permissions Reference](#permissions-reference)
- [Complete Example](#complete-example)

---

## Overview

The `opcua.json` file configures the OPC-UA server plugin for OpenPLC Runtime. It defines:

- **Server identity and network settings**
- **Security policies and authentication methods**
- **User accounts and their access roles**
- **PLC variables exposed to OPC-UA clients**

The configuration is stored as a JSON array, allowing multiple OPC-UA server instances (though typically only one is used).

---

## Top-Level Structure

```json
[
  {
    "name": "opcua_server",
    "protocol": "OPC-UA",
    "config": { ... }
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique identifier for this server instance. Used in logs and internal references. |
| `protocol` | string | Must be `"OPC-UA"` to identify this as an OPC-UA configuration. |
| `config` | object | Contains all server configuration (detailed below). |

---

## Server Configuration

The `config.server` section defines the OPC-UA server identity and network settings.

```json
"server": {
  "name": "OpenPLC OPC UA Server",
  "application_uri": "urn:freeopcua:python:server",
  "product_uri": "urn:openplc:runtime:product",
  "endpoint_url": "opc.tcp://localhost:4840/openplc/opcua",
  "security_profiles": [ ... ]
}
```

### Server Identity Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Human-readable server name displayed to OPC-UA clients during discovery. |
| `application_uri` | string | Unique URI identifying this application. Used for certificate validation and client matching. Format: `urn:domain:application:instance` |
| `product_uri` | string | URI identifying the product type. Helps clients identify the server software. |
| `endpoint_url` | string | Network address where the server listens. Format: `opc.tcp://hostname:port/path` |

### Endpoint URL Components

- **Protocol**: Always `opc.tcp://` for OPC-UA binary protocol
- **Hostname**: Use `localhost` for local-only access, `0.0.0.0` or specific IP for network access
- **Port**: Default OPC-UA port is `4840`. Use a different port to avoid conflicts.
- **Path**: Optional path segment (e.g., `/openplc/opcua`)

### Security Profiles

Each security profile defines a connection method with specific security requirements.

```json
"security_profiles": [
  {
    "name": "insecure",
    "enabled": true,
    "security_policy": "None",
    "security_mode": "None",
    "auth_methods": ["Anonymous"],
    "anonymous_role": "viewer"
  },
  {
    "name": "signed",
    "enabled": true,
    "security_policy": "Basic256Sha256",
    "security_mode": "Sign",
    "auth_methods": ["Username", "Certificate"]
  },
  {
    "name": "signed_encrypted",
    "enabled": true,
    "security_policy": "Basic256Sha256",
    "security_mode": "SignAndEncrypt",
    "auth_methods": ["Username", "Certificate"]
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Profile identifier for logging and reference. |
| `enabled` | boolean | Set `false` to disable this profile without removing it. |
| `security_policy` | string | Cryptographic algorithm suite (see table below). |
| `security_mode` | string | Message protection level (see table below). |
| `auth_methods` | array | Allowed authentication methods for this profile. |
| `anonymous_role` | string | Role granted to Anonymous sessions on this profile: `"viewer"`, `"operator"`, or `"engineer"`. Optional; **defaults to `"viewer"`** (least privilege) when omitted. Only meaningful when `auth_methods` includes `"Anonymous"`. Validated at load — an unknown value is rejected. |

> **Anonymous role.** An anonymous client has no identity, so `anonymous_role` states, explicitly, what it may do — enforced against the per-variable permission matrix exactly like a named user's role. It defaults to `"viewer"` (read-only); raise it to `"operator"` or `"engineer"` only deliberately, since it grants write access to unauthenticated clients. Anonymous sessions cannot be tied to a specific endpoint, so **configure at most one enabled profile with `"Anonymous"`**; if several are enabled the first in list order decides the role (the runtime warns at start).

#### Security Policy Values

| Value | Description | Recommendation |
|-------|-------------|----------------|
| `"None"` | No encryption or signing | Development/testing only |
| `"Basic256Sha256"` | AES-256 encryption, SHA-256 signatures | Recommended for production |
| `"Aes128_Sha256_RsaOaep"` | AES-128 encryption, SHA-256 signatures | Good balance of security/performance |
| `"Aes256_Sha256_RsaPss"` | Latest algorithm suite | Highest security |

#### Security Mode Values

| Value | Description |
|-------|-------------|
| `"None"` | Messages are neither signed nor encrypted |
| `"Sign"` | Messages are signed (integrity protection) but not encrypted |
| `"SignAndEncrypt"` | Messages are both signed and encrypted (confidentiality + integrity) |

#### Authentication Methods

| Value | Description |
|-------|-------------|
| `"Anonymous"` | No credentials required. Use only with `security_policy: "None"` |
| `"Username"` | Username and password authentication |
| `"Certificate"` | X.509 client certificate authentication |

---

## Security Configuration

The `config.security` section manages certificates and trusted clients.

```json
"security": {
  "server_certificate_strategy": "auto_self_signed",
  "server_certificate_custom": null,
  "server_private_key_custom": null,
  "trusted_client_certificates": [ ... ]
}
```

### Certificate Strategy

| Field | Type | Description |
|-------|------|-------------|
| `server_certificate_strategy` | string | How the server obtains its certificate. |
| `server_certificate_custom` | string/null | PEM-encoded certificate (when strategy is `"custom"`). |
| `server_private_key_custom` | string/null | PEM-encoded private key (when strategy is `"custom"`). |

#### Strategy Values

| Value | Description |
|-------|-------------|
| `"auto_self_signed"` | Server generates a self-signed certificate automatically. Easiest setup. |
| `"custom"` | Use certificates provided in `server_certificate_custom` and `server_private_key_custom`. |

### Trusted Client Certificates

For certificate-based authentication, add trusted client certificates here:

```json
"trusted_client_certificates": [
  {
    "id": "engineer_client",
    "pem": "-----BEGIN CERTIFICATE-----\nMIIE8jCCA9qg...\n-----END CERTIFICATE-----"
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique identifier referenced in user configuration. |
| `pem` | string | Full PEM-encoded X.509 certificate (with newlines as `\n`). |

---

## Users Configuration

The `config.users` array defines accounts that can connect to the server.

### Password-Based Users

```json
{
  "type": "password",
  "username": "operator",
  "password_hash": "$2b$12$bb...",
  "role": "operator"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Must be `"password"` for username/password authentication. |
| `username` | string | Login username. |
| `password_hash` | string | Bcrypt-hashed password. Generate with `python -c "import bcrypt; print(bcrypt.hashpw(b'password', bcrypt.gensalt()).decode())"` |
| `role` | string | Permission role: `"viewer"`, `"operator"`, or `"engineer"`. |

### Certificate-Based Users

```json
{
  "type": "certificate",
  "certificate_id": "engineer_client",
  "role": "engineer"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Must be `"certificate"` for X.509 authentication. |
| `certificate_id` | string | References an `id` in `trusted_client_certificates`. |
| `role` | string | Permission role: `"viewer"`, `"operator"`, or `"engineer"`. |

### User Roles

| Role | Description | Typical Use |
|------|-------------|-------------|
| `viewer` | Read-only access to variables with `"r"` permission | Monitoring dashboards, HMI displays |
| `operator` | Read/write access for operational variables | Machine operators, shift supervisors |
| `engineer` | Full access to all variables | System integrators, maintenance |

---

## Cycle Time

```json
"cycle_time_ms": 100
```

| Field | Type | Description |
|-------|------|-------------|
| `cycle_time_ms` | integer | How often (in milliseconds) the plugin synchronizes data with the PLC. |

**Behavior:**
- Lower values = faster updates but higher CPU usage
- Higher values = slower updates but lower resource consumption
- Recommended range: 50-500ms depending on application requirements
- This is independent of the PLC scan cycle time

---

## Address Space

The `config.address_space` section defines what PLC data is exposed to OPC-UA clients.

```json
"address_space": {
  "namespace_uri": "urn:openplc:opcua:datatype:test",
  "variables": [ ... ],
  "structures": [ ... ],
  "arrays": [ ... ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `namespace_uri` | string | Unique URI for this address space. Clients use this to identify your variables. The namespace index is assigned automatically by the server. |
| `variables` | array | Simple scalar variables. |
| `structures` | array | Grouped variables (like PLC structs). |
| `arrays` | array | Array variables with multiple elements. |

---

### Variables

Simple scalar variables are the most common type. Each variable maps to a single PLC memory location.

```json
{
  "node_id": "PLC.Test.simple_int",
  "browse_name": "simple_int",
  "display_name": "Simple Int",
  "datatype": "INT",
  "initial_value": 0,
  "description": "16-bit signed integer test variable",
  "index": 4,
  "permissions": {"viewer": "r", "operator": "rw", "engineer": "rw"}
}
```

| Field | Type | Description |
|-------|------|-------------|
| `node_id` | string | Unique OPC-UA node identifier. Use dot notation for hierarchy (e.g., `PLC.Motor.Speed`). |
| `browse_name` | string | Short name for browsing the address space. Usually the last segment of `node_id`. |
| `display_name` | string | Human-readable name shown in OPC-UA clients. Can include spaces. |
| `datatype` | string | PLC/OPC-UA data type (see [Data Types Reference](#data-types-reference)). |
| `initial_value` | varies | Value used when PLC starts. Type must match `datatype`. |
| `description` | string | Documentation shown to OPC-UA clients. |
| `index` | integer | **Critical**: Maps to the PLC variable buffer index. Must be unique across all variables. |
| `permissions` | object | Access rights per role (see [Permissions Reference](#permissions-reference)). |

#### Node ID Best Practices

- Use hierarchical naming: `PLC.Area.Device.Variable`
- Avoid special characters except dots and underscores
- Keep names consistent with PLC program variable names

#### Index Mapping

The `index` field is crucial for data exchange with the PLC:

- Each index corresponds to a position in the shared memory buffer
- Indices must be unique across variables, structures, and arrays
- Plan your index allocation to leave room for expansion
- Arrays consume consecutive indices (e.g., an 8-element array starting at index 50 uses indices 50-57)

---

### Structures

Structures group related variables together, appearing as folders in OPC-UA clients.

```json
{
  "node_id": "PLC.Test.Structures.sensor1",
  "browse_name": "sensor1",
  "display_name": "Sensor 1",
  "description": "Sensor data structure instance 1",
  "fields": [
    {
      "name": "sensor_id",
      "datatype": "INT",
      "initial_value": 1,
      "index": 20,
      "permissions": {"viewer": "r", "operator": "rw", "engineer": "rw"}
    },
    {
      "name": "value",
      "datatype": "REAL",
      "initial_value": 0.0,
      "index": 21,
      "permissions": {"viewer": "r", "operator": "rw", "engineer": "rw"}
    },
    {
      "name": "is_valid",
      "datatype": "BOOL",
      "initial_value": false,
      "index": 22,
      "permissions": {"viewer": "r", "operator": "r", "engineer": "r"}
    }
  ]
}
```

#### Structure-Level Fields

| Field | Type | Description |
|-------|------|-------------|
| `node_id` | string | Unique identifier for the structure node. |
| `browse_name` | string | Short name for browsing. |
| `display_name` | string | Human-readable name. |
| `description` | string | Documentation for the structure. |
| `fields` | array | Array of field definitions (each similar to a variable). |

#### Field-Level Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Field name within the structure. |
| `datatype` | string | Data type of this field. |
| `initial_value` | varies | Initial value when PLC starts. |
| `index` | integer | PLC buffer index for this field. |
| `permissions` | object | Access rights per role. |

**Note:** Structure fields can have different permissions, allowing some fields to be read-only while others are writable.

---

### Arrays

Arrays expose multiple values under a single variable, useful for buffers, sensor banks, or I/O modules.

```json
{
  "node_id": "PLC.Test.Arrays.int_array",
  "browse_name": "int_array",
  "display_name": "Int Array",
  "datatype": "INT",
  "length": 5,
  "initial_value": 0,
  "index": 58,
  "permissions": {"viewer": "r", "operator": "rw", "engineer": "rw"}
}
```

| Field | Type | Description |
|-------|------|-------------|
| `node_id` | string | Unique identifier for the array node. |
| `browse_name` | string | Short name for browsing. |
| `display_name` | string | Human-readable name. |
| `datatype` | string | Data type of each element. |
| `length` | integer | Number of elements in the array. |
| `initial_value` | varies | Initial value applied to ALL elements. |
| `index` | integer | Starting PLC buffer index. Elements use `index`, `index+1`, ..., `index+length-1`. |
| `permissions` | object | Access rights (applies to all elements). |

**Index Allocation Example:**
An array with `"index": 58` and `"length": 5` uses indices 58, 59, 60, 61, 62.

---

## Data Types Reference

| Type | OPC-UA Type | Size | Range | Initial Value Example |
|------|-------------|------|-------|----------------------|
| `BOOL` | Boolean | 1 bit | `true` / `false` | `false` |
| `BYTE` | Byte | 8 bits | 0 to 255 | `0` |
| `INT` | Int16 | 16 bits | -32,768 to 32,767 | `0` |
| `DINT` | Int32 | 32 bits | -2,147,483,648 to 2,147,483,647 | `0` |
| `LINT` | Int64 | 64 bits | -9.2e18 to 9.2e18 | `0` |
| `REAL` | Float | 32 bits | IEEE 754 single precision | `0.0` |
| `STRING` | String | Variable | UTF-8 text | `""` |

---

## Permissions Reference

Permissions are defined per role using a simple string notation:

| Permission | Meaning |
|------------|---------|
| `"r"` | Read-only access |
| `"rw"` | Read and write access |
| `""` or omitted | No access |

### Example Permission Configurations

**Status variable (read-only for everyone):**
```json
"permissions": {"viewer": "r", "operator": "r", "engineer": "r"}
```

**Setpoint (operators and engineers can modify):**
```json
"permissions": {"viewer": "r", "operator": "rw", "engineer": "rw"}
```

**Configuration parameter (engineers only):**
```json
"permissions": {"viewer": "", "operator": "r", "engineer": "rw"}
```

---

## Complete Example

Here's a minimal but complete configuration:

```json
[
  {
    "name": "opcua_server",
    "protocol": "OPC-UA",
    "config": {
      "server": {
        "name": "My PLC OPC-UA Server",
        "application_uri": "urn:mycompany:plc:server",
        "product_uri": "urn:mycompany:plc:product",
        "endpoint_url": "opc.tcp://0.0.0.0:4840/plc",
        "security_profiles": [
          {
            "name": "secure",
            "enabled": true,
            "security_policy": "Basic256Sha256",
            "security_mode": "SignAndEncrypt",
            "auth_methods": ["Username"]
          }
        ]
      },
      "security": {
        "server_certificate_strategy": "auto_self_signed",
        "server_certificate_custom": null,
        "server_private_key_custom": null,
        "trusted_client_certificates": []
      },
      "users": [
        {
          "type": "password",
          "username": "admin",
          "password_hash": "$2b$12$...",
          "role": "engineer"
        }
      ],
      "cycle_time_ms": 100,
      "address_space": {
        "namespace_uri": "urn:mycompany:plc:variables",
        "variables": [
          {
            "node_id": "PLC.Motor.Speed",
            "browse_name": "Speed",
            "display_name": "Motor Speed",
            "datatype": "REAL",
            "initial_value": 0.0,
            "description": "Current motor speed in RPM",
            "index": 0,
            "permissions": {"viewer": "r", "operator": "r", "engineer": "r"}
          },
          {
            "node_id": "PLC.Motor.Setpoint",
            "browse_name": "Setpoint",
            "display_name": "Speed Setpoint",
            "datatype": "REAL",
            "initial_value": 0.0,
            "description": "Target motor speed in RPM",
            "index": 1,
            "permissions": {"viewer": "r", "operator": "rw", "engineer": "rw"}
          }
        ],
        "structures": [],
        "arrays": []
      }
    }
  }
]
```

---

## Troubleshooting

### Common Issues

1. **Client cannot connect**
   - Verify `endpoint_url` is reachable from the client
   - Check firewall rules for the configured port
   - Ensure at least one `security_profile` is enabled

2. **Authentication fails**
   - Verify password hash is valid bcrypt format
   - For certificate auth, ensure certificate is in `trusted_client_certificates`
   - Check that `auth_methods` includes the method the client is using

3. **Variables not updating**
   - Verify `index` values match PLC program memory locations
   - Check `cycle_time_ms` is appropriate for your update rate needs
   - Ensure the PLC program is running

4. **Permission denied errors**
   - Check user's `role` matches required permissions
   - Verify variable `permissions` include the user's role
   - Ensure the operation (read/write) is allowed for that role
