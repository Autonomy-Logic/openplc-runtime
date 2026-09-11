# OpenPLC Editor Integration

This document describes how the OpenPLC Editor communicates with the OpenPLC Runtime v4.

## Overview

The OpenPLC Runtime v4 is a **headless service** designed to be controlled by the OpenPLC Editor desktop application. There is **no web browser interface** for end users. All interaction with the runtime happens through the OpenPLC Editor via a REST API on port 8443.

## Architecture

```
OpenPLC Editor (Desktop App)
    |
    | HTTPS (port 8443)
    | JWT Authentication
    |
    v
OpenPLC Runtime v4 (Headless Service)
    - REST API Server
    - PLC Runtime Core
    - Compilation Engine
```

## Workflow

### 1. User Creates Program in Editor

Users create PLC programs using the OpenPLC Editor's graphical interface (Ladder, FBD, SFC) or text-based languages (ST, IL).

### 2. Local Compilation

When the user clicks "Compile" in the Editor, the following happens locally on the user's machine:

1. Editor converts project JSON to XML format
2. XML is transpiled to Structured Text (ST) using xml2st
3. ST is transpiled to C code using iec2c
4. C/C++ blocks are generated
5. Debug files and glue variables are created
6. For Runtime v4: All source files are compressed into `program.zip`
7. For Runtime v3: Only `program.st` is prepared

### 3. Authentication

Before uploading, the Editor must authenticate with the runtime:

**First-time setup:**
```
POST /api/create-user
{
  "username": "admin",
  "password": "password",
  "role": "user"
}
```

**Login:**
```
POST /api/login
{
  "username": "admin",
  "password": "password"
}

Response:
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc..."
}
```

The Editor stores this JWT token and includes it in all subsequent requests as:
```
Authorization: Bearer <jwt_token>
```

### 4. Program Upload

The Editor uploads the compiled program to the runtime:

**Endpoint:** `POST /api/upload-file`

**Request:**
- Method: POST
- Content-Type: multipart/form-data
- Authorization: Bearer <jwt_token>
- Body: file field containing program.zip (Runtime v4) or program.st (Runtime v3)

**Response:**
```json
{
  "UploadFileFail": "",
  "CompilationStatus": "COMPILING"
}
```

### 5. Compilation Monitoring

The runtime compiles the uploaded program asynchronously. The Editor polls for status:

**Endpoint:** `GET /api/compilation-status`

**Polling Configuration:**
- Interval: 1 second
- Timeout: 5 minutes (300 seconds)

**Response:**
```json
{
  "status": "COMPILING",
  "logs": [
    "[INFO] Starting compilation",
    "[INFO] Compiling source files...",
    "..."
  ],
  "exit_code": null
}
```

**Status Values:**
- `IDLE` - No compilation in progress
- `UNZIPPING` - Extracting uploaded ZIP file
- `COMPILING` - Running compilation scripts
- `SUCCESS` - Compilation completed successfully
- `FAILED` - Compilation failed

### 6. PLC Control

Once compiled, the Editor can control PLC execution:

**Start PLC:**
```
GET /api/start-plc
Authorization: Bearer <jwt_token>

Response:
{
  "status": "RUNNING"
}
```

**Stop PLC:**
```
GET /api/stop-plc
Authorization: Bearer <jwt_token>

Response:
{
  "status": "STOPPED"
}
```

**Get Status:**
```
GET /api/status
Authorization: Bearer <jwt_token>

Response:
{
  "status": "RUNNING"
}
```

### 7. Debugging

The Editor connects to the runtime's WebSocket debug interface for real-time variable monitoring and forcing:

**Connection:**
```javascript
import { io } from 'socket.io-client';

const socket = io('https://runtime-ip:8443', {
  path: '/socket.io',
  transports: ['websocket'],
  auth: {
    token: jwt_token
  },
  rejectUnauthorized: false  // Editor handles self-signed certs
});

// Connect to debug namespace
socket.on('connect', () => {
  socket.emit('join', { namespace: '/api/debug' });
});

// Listen for debug responses
socket.on('debug_response', (data) => {
  console.log('Debug response:', data);
});

// Send debug commands
socket.emit('debug_command', {
  command: '45 00 00'  // Hex-encoded debug command
});
```

See [DEBUG_PROTOCOL.md](DEBUG_PROTOCOL.md) for detailed WebSocket protocol documentation.

## TLS/Certificate Handling

The runtime uses self-signed TLS certificates by default. The OpenPLC Editor handles this by:

1. Setting `rejectUnauthorized: false` in HTTPS requests
2. Using the `-k` flag equivalent in HTTP client libraries
3. Optionally allowing users to configure `RUNTIME_TLS_REJECT_UNAUTHORIZED` environment variable

**Note:** This is an Editor-side configuration, not a runtime configuration.

## API Endpoints Summary

### Version and compatibility
- `GET /api/version` - Runtime version string
- `GET /api/capabilities` - Runtime version plus `minEditorVersion`, the oldest
  editor this runtime accepts programs from

Both are unauthenticated: the Editor calls them before login to decide whether
it may talk to this device. `minEditorVersion` is **advertised, not enforced** —
the Editor compares it against its own version and refuses to upload, so a
runtime release can never lock out an editor already installed in the field.
Editors that predate `/api/capabilities` get a 404 and fall back to
`/api/version`, seeing no editor floor (their previous behaviour).

### Authentication
- `POST /api/create-user` - Create user account
- `POST /api/login` - Login and get JWT token
- `POST /api/logout` - Logout and revoke JWT token
- `GET /api/get-users-info` - Check if users exist
- `GET /api/get-user-info/<user_id>` - Get user information
- `PUT /api/update-user/<user_id>` - Update a user's username, password or role (admin, or your own account)
- `DELETE /api/delete-user/<user_id>` - Delete user

### PLC Operations
- `POST /api/upload-file` - Upload program ZIP file
- `GET /api/compilation-status` - Get compilation status and logs
- `GET /api/status` - Get PLC runtime status
- `GET /api/start-plc` - Start PLC execution
- `GET /api/stop-plc` - Stop PLC execution
- `GET /api/ping` - Ping runtime
- `GET /api/runtime-logs?id=<min_id>&level=<level>` - Get runtime logs

### Debug Interface
- `wss://host:8443/api/debug` - WebSocket debug interface

All endpoints except `/api/version`, `/api/capabilities`, `/api/create-user` (first user only), `/api/login`, and `/api/get-users-info` require JWT authentication.

## Error Handling

The Editor handles various error conditions:

**Upload Errors:**
- File too large (>10 MB per file, >50 MB total)
- Invalid ZIP file
- Compilation already in progress
- Path traversal attempts
- Disallowed file extensions (.exe, .dll, .sh, .bat, .js, .vbs, .scr)

**Compilation Errors:**
- Syntax errors in ST code
- Missing dependencies
- Compilation timeout (5 minutes)

**Connection Errors:**
- Runtime not reachable
- Invalid JWT token
- Certificate validation errors

## Development and Testing

For developers integrating with the runtime API, see [API.md](API.md) for detailed endpoint documentation and curl examples.

For runtime developers, see [DEVELOPMENT.md](DEVELOPMENT.md) for local development setup.

## Security Considerations

- All API requests require HTTPS (port 8443)
- JWT tokens are required for all operations except initial user creation and login
- Tokens can be revoked via logout
- File uploads are validated for size, compression ratio, and file extensions
- Path traversal protection is enforced during ZIP extraction

See [SECURITY.md](SECURITY.md) for comprehensive security documentation.
