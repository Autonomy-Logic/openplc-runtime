# REST API Reference

## Overview

The OpenPLC Runtime v4 provides an internal REST API over HTTPS used by the OpenPLC Editor desktop application. The API is not intended for direct end-user interaction but can be used for advanced integration or diagnostics.

All endpoints are accessible at `https://<host>:8443/api/<endpoint>`.

## Base URL

```
https://localhost:8443/api
```

## Authentication

The runtime uses JWT-based authentication:

1. **First User Creation**: `POST /api/create-user` (no authentication required for the first user)
2. **Login**: `POST /api/login` returns a JWT access token
3. **Authenticated Requests**: All other endpoints require `Authorization: Bearer <token>` header
4. **Renewal**: `POST /api/renew` returns a new token for the same session before the current one
   expires (15 minutes); the presented token is revoked

### Roles

| Role | Reaches |
| --- | --- |
| `admin` | Everything, plus account management (`create-user`, `delete-user`, role changes) and `project-snapshot` |
| `engineer` | PLC operation: upload, start/stop, logs, compilation status, serial ports, plugin commands, discovery, the debugger (`/api/debug`), the user list, its own account |
| `operator`, `viewer` | Only `login`, `logout`, `renew`, `whoami`, `GET /api/status` and `/api/ping`, and changing their own password with `update-user` |

Every other request from an `operator` or `viewer` is refused with `403`. The role `user` no
longer exists: stored `user` accounts become `engineer` when the runtime starts, and a request
naming `user` is refused with `400`. The table lives in `webserver/access.py` (`POLICY`).

### Sessions

- Each sign-in is a session. Renewing rotates its token: the old token is refused from then on, and
  presenting it again more than 10 seconds after the renewal ends the whole session.
- `POST /api/logout` ends that session only. A password change, a role change or deleting the
  account ends every session of the account.
- Sessions, revocations and account cut-offs are stored in the runtime database, so they hold
  across restarts.

### Sign-in throttling

Sign-in failures are counted per client address only, whatever the username: failures for many
usernames from one address share one counter, and the same username from two addresses has two.
After a failure, the next attempt from that address is accepted only after 5 seconds; each further
failure doubles the wait, up to 5 minutes. Failures from one address never delay a sign-in from
another. Callers whose address is unknown share one counter. An attempt made too early is answered
`429 Too Many Requests` with a `Retry-After` header (seconds). A successful sign-in does not clear
the counter; it starts again from 5 seconds once the address has had no failure for an hour. The
`current_password` check of `PUT /api/update-user` counts against the same counter. The
bootloader's `/api/bootloader/login` follows the same rule with its own counters.

**Note**: The OpenPLC Editor handles authentication automatically. Advanced integrators must implement the authentication flow manually.

## Common Response Format

All API responses are JSON objects. Successful responses typically include a `status` field, while errors include descriptive error messages.

## Authentication Endpoints

### Create User

Create a new user account. The first user can be created without authentication. Subsequent users require JWT authentication.

**Request:**
```http
POST /api/create-user
Content-Type: application/json

{
  "username": "admin",
  "password": "your_password",
  "role": "admin"
}
```

**Response (Success):**
```json
{
  "msg": "User created",
  "id": 1
}
```

**Response (Error):**
```json
{
  "msg": "Username already exists"
}
```

**Status Codes:**
- `201 Created` - User created successfully
- `400 Bad Request` - Missing username or password
- `401 Unauthorized` - User already exists and no valid JWT provided
- `409 Conflict` - Username already exists

---

### Login

Authenticate and receive a JWT access token.

**Request:**
```http
POST /api/login
Content-Type: application/json

{
  "username": "admin",
  "password": "your_password"
}
```

**Response (Success):**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

**Response (Error):**
```json
"Wrong username or password"
```

**Status Codes:**
- `200 OK` - Login successful
- `401 Unauthorized` - Invalid credentials
- `429 Too Many Requests` - Sign-in throttled; retry after `Retry-After` seconds
- `500 Internal Server Error` - Database error

**Notes:**
- The access token should be included in the `Authorization: Bearer <token>` header for all subsequent requests
- Tokens expire after 15 minutes; renew with `POST /api/renew` before that

---

## PLC Control Endpoints

All PLC control endpoints require JWT authentication.

### Start PLC

Start the PLC program execution.

**Request:**
```http
GET /api/start-plc
Authorization: Bearer <token>
```

**Response:**
```json
{
  "status": "START:OK"
}
```

`status` is the runtime's reply to the `START` socket command, forwarded verbatim. The webserver
waits up to 15 s for it.

**Possible Status Values:**
- `"START:OK"` - The start was accepted. While the runtime process has a switch owner registered,
  the reply comes once the switch gate let the start through, after the plugin re-initialisation
  and the wait of up to 2 s, and before the PLC cycle thread is created; a failure after that point
  lands ERROR, which `/api/status` reports. A switch owner is registered once the process has
  initialised a switch owner plugin from its plugin configuration, at boot or during an earlier
  start, and that plugin declared ownership (`owns_mode_switch`) or reported a position; it stays
  registered until a start loads a plugin configuration without it. With no switch owner
  registered, the reply comes as soon as the start begins.

  The exception is the first START after an upload whose plugin configuration adds a switch owner
  plugin, on a process with no switch owner registered yet (for example a freshly installed
  runtime, or one that was running a project without such a plugin). That START replies
  `START:OK` at once, and the switch gate can then refuse it before the first scan: no scan runs,
  the PLC lands STOPPED, and `/api/status` reports `"STOPPED"`. The reason is the log line, which
  a client reads from `GET /api/runtime-logs`: `Start refused: the mode switch is in STOP` or
  `Start refused: no switch owner has reported a position within 2000 ms`, whatever the switch
  owner. `switchPosition` alone does not tell the reason. It reads `"stop"` (switch in STOP) only
  when every switch owner stays attached across the rollback, that is, its `detach_image`
  accepts. An owner without `detach_image`, or whose `detach_image` refuses, is cleaned up on the
  rollback, its report stops counting, and `switchPosition` then reads `"unknown"` whatever the
  switch position. So after this refusal `"unknown"` means either no report within 2 s or a
  cleaned-up owner. Later STARTs in the same process reply with the gate's decision. A client
  that needs to know whether the PLC is running after `START:OK` polls `/api/status`.
- `"START:ERROR_ALREADY_RUNNING"` - PLC was already in RUNNING state
- `"START:ERROR_SWITCH_STOP"` - The run/stop mode switch was in STOP before the start began, or
  the switch owner reported STOP after the start re-initialised the plugins; no scan ran and the
  PLC ends STOPPED. After a refusal at the re-initialisation, the reply comes before the rollback
  ends, so `/api/status` can report the transition briefly until the PLC lands STOPPED
- `"START:ERROR_SWITCH_UNKNOWN"` - A switch owner plugin did not report a position within 2 s
  of the start re-initialising the plugins; no scan ran and the PLC ends STOPPED. The reply
  comes before the rollback ends, so `/api/status` can report the transition briefly until the
  PLC lands STOPPED
- `"START:ERROR"` - The start could not begin or failed (another state change in flight, no
  program, a failed load, or the start did not land within 12 s)
- `null` - Runtime process not responding

---

### Stop PLC

Stop the PLC program execution.

**Request:**
```http
GET /api/stop-plc
Authorization: Bearer <token>
```

**Response:**
```json
{
  "status": "PLC stopped successfully"
}
```

**Possible Status Values:**
- `"PLC stopped successfully"` - PLC transitioned to STOPPED state
- `"PLC is already stopped"` - PLC was already in STOPPED state
- `"No response from runtime"` - Runtime process not responding

---

### Get PLC Status

Query the current PLC state.

**Request:**
```http
GET /api/status
Authorization: Bearer <token>
```

**Response:**
```json
{
  "status": "RUNNING",
  "switchPosition": "run"
}
```

`switchPosition` is the run/stop mode-switch position:
- `"run"` - The switch is in RUN, or no plugin owns a switch
- `"stop"` - The switch is in STOP; every start is refused
- `"unknown"` - A switch owner plugin has not reported a position since its last init; every
  start is refused until it does

The licence state is not reported here: the editor reads the stored licences from
[`GET /api/runtime-license`](#read-runtime-licences) and verifies them.

**Possible Status Values:**
- `"EMPTY"` - No PLC program loaded
- `"INIT"` - Program loaded, initializing
- `"RUNNING"` - Actively executing scan cycles
- `"STOPPED"` - Program loaded but not executing
- `"ERROR"` - Recoverable error state
- `"No response from runtime"` - Runtime process not responding

---

### Ping Runtime

Check if the runtime process is responsive.

**Request:**
```http
GET /api/ping
Authorization: Bearer <token>
```

**Response:**
```json
{
  "status": "pong"
}
```

**Possible Status Values:**
- `"pong"` - Runtime is responsive
- `null` - Runtime not responding

---

## Program Management Endpoints

### Upload PLC Program

Upload a ZIP file containing the PLC program source files generated by OpenPLC Editor v4.

**Request:**
```http
POST /api/upload-file
Authorization: Bearer <token>
Content-Type: multipart/form-data

file: <ZIP file>
```

**Success Response:**
```json
{
  "UploadFileFail": "",
  "CompilationStatus": "COMPILING"
}
```

**Error Response:**
```json
{
  "UploadFileFail": "Error message",
  "CompilationStatus": "FAILED"
}
```

**Compilation Status Values:**
- `"IDLE"` - No build in progress
- `"UNZIPPING"` - Extracting ZIP file
- `"COMPILING"` - Running compilation scripts
- `"SUCCESS"` - Build completed successfully
- `"FAILED"` - Build failed

**Error Conditions:**
- No file in request
- File too large (>10 MB per file, >50 MB total)
- ZIP validation failed (path traversal, compression ratio, disallowed extensions)
- Another compilation in progress
- File system error

**Notes:**
- Compilation runs asynchronously in a background thread
- Use the `/api/compilation-status` endpoint to monitor progress
- The PLC is automatically stopped during compilation
- The OpenPLC Editor compiles the program locally (JSON → XML → ST → C) and uploads the source files as a ZIP

---

### Get Compilation Status

Query the status of the most recent compilation.

**Request:**
```http
GET /api/compilation-status
Authorization: Bearer <token>
```

**Response:**
```json
{
  "status": "SUCCESS",
  "logs": [
    "[INFO] Starting compilation",
    "[INFO] Compiling Config0.c...",
    "[INFO] Compiling Res0.c...",
    "[INFO] Compiling debug.c...",
    "[INFO] Compiling glueVars.c...",
    "[INFO] Compiling c_blocks_code.cpp...",
    "[INFO] Compiling shared library...",
    "[INFO] Build finished successfully"
  ],
  "exit_code": 0
}
```

**Response Fields:**
- `status` - Current build status (IDLE, UNZIPPING, COMPILING, SUCCESS, FAILED)
- `logs` - Array of log messages from the build process
- `exit_code` - Exit code of the compilation script (0 = success, non-zero = error, null = in progress)

**Notes:**
- The OpenPLC Editor polls this endpoint to monitor compilation progress
- Logs are accumulated during compilation
- Error messages are prefixed with `[ERROR]`
- Exit code is `null` until compilation completes

---

### Get Runtime Logs

Retrieve logs from the PLC runtime process.

**Request:**
```http
GET /api/runtime-logs
GET /api/runtime-logs?id=<min_id>
GET /api/runtime-logs?level=<log_level>
Authorization: Bearer <token>
```

**Query Parameters:**
- `id` (optional) - Minimum log ID to retrieve (for pagination)
- `level` (optional) - Filter by log level (DEBUG, INFO, WARNING, ERROR)

**Response:**
```json
{
  "runtime-logs": [
    {
      "id": 1,
      "timestamp": "2024-01-01T12:00:00.000Z",
      "level": "INFO",
      "message": "PLC started successfully"
    },
    {
      "id": 2,
      "timestamp": "2024-01-01T12:00:05.000Z",
      "level": "DEBUG",
      "message": "Scan Count: 100"
    }
  ]
}
```

**Log Levels:**
- `DEBUG` - Detailed diagnostic information
- `INFO` - General informational messages
- `WARNING` - Warning messages
- `ERROR` - Error messages

---

## Runtime Licence

OpenPLC Runtime is free under the GPLv3. A commercial licence is a JSON file Autonomy Edge signs for
one hardware ID v2 ([HARDWARE_ID.md](HARDWARE_ID.md)). The runtime stores one licence and reports it
with its hardware ID; it never checks a signature, never contacts Edge and never limits anything.
The editor fetches licences from Edge, uploads them, and decides the GPLv3 or commercial status by
verifying what the runtime reports.

### Read Runtime Licences

```http
GET /api/runtime-license
```

Public: no login (runtimes before this change answer 401).

**Response (200):**
```json
{
  "hardwareId": "hw2:...",
  "licenses": [
    {"fmt":1,"product":"com.autonomy.openplc-runtime","hardware_id":"hw2:...","seats":2,
     "order_id":"...","issued_at":"2026-10-28T12:00:00.000Z","key_id":0,"sig":"..."}
  ]
}
```

`hardwareId` is as in `GET /api/hardware-id`
([HARDWARE_ID.md](HARDWARE_ID.md#record-and-storage)), `null` when no ID can be derived.
`licenses` holds the one stored licence exactly as stored, unverified, or nothing; it may be for
another hardware ID. The editor counts a licence only when its `hardware_id` equals `hardwareId` and its
signature verifies: ECDSA P-256 over SHA-256 of the payload without `sig`, keys sorted, no
whitespace, UTF-8, with `sig` the raw r and s (64 bytes) in base64 and the public key selected by
`key_id`.

### Upload Runtime Licence

```http
POST /api/runtime-license
Authorization: Bearer <token>
Content-Type: application/json

{"fmt":1,"product":"com.autonomy.openplc-runtime","hardware_id":"hw2:...","seats":2,
 "order_id":"...","issued_at":"2026-10-28T12:00:00.000Z","key_id":0,"sig":"..."}
```

Any authenticated user. The body is the licence file exactly as Edge serves it, at most 4 KiB. Only
its shape is checked (exact field set, `product`, `fmt`, value ranges); it replaces the stored
licence.

**Response (200):**
```json
{"orderId": "..."}
```

| Status | Meaning |
|--------|---------|
| 400 | Not a runtime licence |
| 401 | Authentication required |
| 413 | Body larger than 4 KiB |
| 500 | The licence could not be written |

The licence lives in `runtime.license` in the persistent data directory, on a device and in a vPLC
alike (`/var/run/runtime/runtime.license` in a container). In a vPLC, orchestrator-agent empties that
file on `remove_license`; an empty, malformed or oversized file reads as no licence.

### Remove Runtime Licence

```http
DELETE /api/runtime-license
Authorization: Bearer <token>
```

Any authenticated user. **Response (200):** `{"removed": true}` when the slot held a licence, or
`false` when it was missing, empty or malformed (the file is deleted either way). 500 when the file
could not be removed.

---

## EtherCAT Discovery Endpoints

The `/api/discovery/ethercat/*` endpoints are listed in [ETHERCAT.md](ETHERCAT.md#discovery-endpoints).

## Error Handling

### HTTP Status Codes

- `200 OK` - Request successful
- `201 Created` - Resource created successfully
- `400 Bad Request` - Invalid request parameters
- `401 Unauthorized` - Authentication required or invalid token
- `409 Conflict` - Resource conflict (e.g., username already exists)
- `500 Internal Server Error` - Server error

### Error Response Format

```json
{
  "error": "Error message description"
}
```

Or for some endpoints:

```json
{
  "msg": "Error message description"
}
```

## Usage Examples

### Complete Authentication Flow

**cURL Examples:**

```bash
# Step 1: Create first user
curl -k -X POST https://localhost:8443/api/create-user \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123","role":"admin"}'

# Step 2: Login and get JWT token
TOKEN=$(curl -k -X POST https://localhost:8443/api/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' \
  | jq -r '.access_token')

echo "Token: $TOKEN"

# Step 3: Use token for authenticated requests

# Get PLC status
curl -k https://localhost:8443/api/status \
  -H "Authorization: Bearer $TOKEN"

# Start PLC
curl -k https://localhost:8443/api/start-plc \
  -H "Authorization: Bearer $TOKEN"

# Stop PLC
curl -k https://localhost:8443/api/stop-plc \
  -H "Authorization: Bearer $TOKEN"

# Upload program
curl -k -X POST https://localhost:8443/api/upload-file \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@program.zip"

# Get compilation status
curl -k https://localhost:8443/api/compilation-status \
  -H "Authorization: Bearer $TOKEN"

# Get runtime logs
curl -k https://localhost:8443/api/runtime-logs \
  -H "Authorization: Bearer $TOKEN"

# Ping runtime
curl -k https://localhost:8443/api/ping \
  -H "Authorization: Bearer $TOKEN"
```

**Note:** The `-k` flag bypasses certificate verification for self-signed certificates.

**Python Example:**

```python
import requests
import urllib3
import time

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

base_url = "https://localhost:8443/api"

# Step 1: Create first user (no auth required)
response = requests.post(
    f"{base_url}/create-user",
    json={"username": "admin", "password": "admin123", "role": "admin"},
    verify=False
)
print(f"User creation: {response.json()}")

# Step 2: Login to get JWT token
response = requests.post(
    f"{base_url}/login",
    json={"username": "admin", "password": "admin123"},
    verify=False
)
token = response.json()["access_token"]
print(f"Received token: {token[:50]}...")

# Step 3: Use token for authenticated requests
headers = {"Authorization": f"Bearer {token}"}

# Get PLC status
response = requests.get(f"{base_url}/status", headers=headers, verify=False)
print(f"PLC Status: {response.json()}")

# Start PLC
response = requests.get(f"{base_url}/start-plc", headers=headers, verify=False)
print(f"Start PLC: {response.json()}")

# Upload program
with open("program.zip", "rb") as f:
    files = {"file": f}
    response = requests.post(
        f"{base_url}/upload-file",
        files=files,
        headers=headers,
        verify=False
    )
    print(f"Upload: {response.json()}")

# Monitor compilation
while True:
    response = requests.get(
        f"{base_url}/compilation-status",
        headers=headers,
        verify=False
    )
    status = response.json()
    print(f"Compilation Status: {status['status']}")

    if status["status"] in ["SUCCESS", "FAILED"]:
        print("Compilation Logs:")
        for log in status["logs"]:
            print(f"  {log}")
        break

    time.sleep(1)
```

**JavaScript/Node.js Example:**

```javascript
const https = require('https');
const fetch = require('node-fetch');
const FormData = require('form-data');
const fs = require('fs');

// Disable SSL verification for self-signed certificates
const agent = new https.Agent({ rejectUnauthorized: false });
const baseUrl = "https://localhost:8443/api";

async function main() {
  // Step 1: Create first user
  let response = await fetch(`${baseUrl}/create-user`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: 'admin',
      password: 'admin123',
      role: 'admin'
    }),
    agent
  });
  console.log('User creation:', await response.json());

  // Step 2: Login to get JWT token
  response = await fetch(`${baseUrl}/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: 'admin',
      password: 'admin123'
    }),
    agent
  });
  const { access_token } = await response.json();
  console.log('Received token:', access_token.substring(0, 50) + '...');

  // Step 3: Use token for authenticated requests
  const headers = { 'Authorization': `Bearer ${access_token}` };

  // Get PLC status
  response = await fetch(`${baseUrl}/status`, { headers, agent });
  console.log('PLC Status:', await response.json());

  // Start PLC
  response = await fetch(`${baseUrl}/start-plc`, { headers, agent });
  console.log('Start PLC:', await response.json());

  // Upload program
  const formData = new FormData();
  formData.append('file', fs.createReadStream('program.zip'));

  response = await fetch(`${baseUrl}/upload-file`, {
    method: 'POST',
    headers: { ...headers },
    body: formData,
    agent
  });
  console.log('Upload:', await response.json());

  // Monitor compilation
  while (true) {
    response = await fetch(`${baseUrl}/compilation-status`, { headers, agent });
    const status = await response.json();
    console.log('Compilation Status:', status.status);

    if (status.status === 'SUCCESS' || status.status === 'FAILED') {
      console.log('Compilation Logs:');
      status.logs.forEach(log => console.log('  ' + log));
      break;
    }

    await new Promise(resolve => setTimeout(resolve, 1000));
  }
}

main();
```

## Rate Limiting

Currently, there are no rate limits enforced. However, be mindful of:
- Only one compilation can run at a time
- Frequent start/stop commands may cause state transition issues
- Log queries can be resource-intensive with large log volumes

## Security Considerations

1. **HTTPS Only**: All communication must use HTTPS (port 8443)
2. **JWT Authentication**: All endpoints except first user creation and login require JWT authentication
3. **Self-Signed Certificates**: Default installation uses self-signed certificates (OpenPLC Editor handles this automatically)
4. **File Upload Validation**: ZIP files undergo comprehensive security checks before extraction
5. **Size Limits**: Files are limited to prevent resource exhaustion (10 MB per file, 50 MB total)
6. **Path Validation**: All file paths are validated to prevent traversal attacks
7. **Password Security**: Passwords are hashed with PBKDF2-SHA256 (600,000 iterations), salt, and pepper

## WebSocket Debug Interface

For real-time debugging and variable inspection, the OpenPLC Editor uses a WebSocket interface at `https://<host>:8443/api/debug`. See [Debug Protocol](DEBUG_PROTOCOL.md) for details.

## Related Documentation

- [Editor Integration](EDITOR_INTEGRATION.md) - How OpenPLC Editor uses this API
- [Architecture](ARCHITECTURE.md) - System overview
- [Debug Protocol](DEBUG_PROTOCOL.md) - WebSocket debug interface
- [Compilation Flow](COMPILATION_FLOW.md) - Build process details
- [Security](SECURITY.md) - Security features
- [Hardware ID](HARDWARE_ID.md) - Hardware ID v2 derivation, storage and `GET /api/hardware-id`
- [Troubleshooting](TROUBLESHOOTING.md) - Common issues
