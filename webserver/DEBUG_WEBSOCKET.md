# WebSocket Debug Protocol

## Overview

The OpenPLC Runtime v4 provides a secure WebSocket endpoint for debugger communication over HTTPS. This allows the OpenPLC Editor to maintain a persistent, authenticated connection for real-time variable polling without the overhead of repeated TLS handshakes.

## Connection

### Endpoint
```
wss://<runtime-host>:8443/api/debug
```

### Authentication
The WebSocket connection requires JWT authentication via query parameter:
```
wss://localhost:8443/api/debug?token=<JWT_ACCESS_TOKEN>
```

The JWT token is obtained through the standard REST API login endpoint:
```bash
POST https://localhost:8443/api/login
Content-Type: application/json

{
  "username": "admin",
  "password": "password"
}
```

Response:
```json
{
  "access_token": "eyJhbGc..."
}
```

## Protocol

### Connection Events

#### `connect`
Emitted by server when connection is successfully established and authenticated.

**Server Response:**
```json
{
  "status": "ok"
}
```

#### `disconnect`
Connection closed (either by client or server).

### Debug Communication

#### `debug_command` (Client → Server)
Send debug command to the runtime.

**Request:**
```json
{
  "command": "41 DE AD 00 00"
}
```

Where `command` is a hex string representing the debug command bytes (same format as Arduino/Modbus implementation).

**Response Event:** `debug_response`

#### `debug_response` (Server → Client)
Response to debug command.

**Success Response:**
```json
{
  "success": true,
  "data": "7E 12 34 56 78"
}
```

**Error Response:**
```json
{
  "success": false,
  "error": "Error message"
}
```

## Debug Command Format

The debug commands follow the same protocol as the Arduino/Modbus implementation:

### Function Codes
- `0x41` - DEBUG_INFO: Get debug information
- `0x42` - DEBUG_SET: Set trace variable
- `0x43` - DEBUG_GET: Get trace data
- `0x44` - DEBUG_GET_LIST: Get list of variable values
- `0x45` - DEBUG_GET_MD5: Get MD5 hash

### Example: Get MD5 Hash
**Request:**
```json
{
  "command": "45 DE AD 00 00"
}
```

**Response (Success):**
```json
{
  "success": true,
  "data": "7E 61 62 63 64 65 66 31 32 33 34 35 36 37 38 39 30 31 32 33 34 35 36 37 38 39 30 31 32 33 34 35 36 00"
}
```

### Example: Get Variables List
**Request:**
```json
{
  "command": "44 00 03 00 00 00 01 00 02"
}
```
(Get 3 variables: indexes 0, 1, 2)

**Response (Success):**
```json
{
  "success": true,
  "data": "7E 00 02 00 00 00 64 00 0A 01 00 01 01"
}
```

## Client Implementation Example (JavaScript)

```javascript
import io from 'socket.io-client';

// Obtain JWT token first
const response = await fetch('https://localhost:8443/api/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: 'admin', password: 'password' })
});
const { access_token } = await response.json();

// Connect to WebSocket with authentication
const socket = io('wss://localhost:8443/api/debug', {
  transports: ['websocket'],
  query: { token: access_token },
  rejectUnauthorized: false  // Only for self-signed certificates in development
});

socket.on('connected', (data) => {
  console.log('Connected:', data);

  // Send debug command
  socket.emit('debug_command', {
    command: '45 DE AD 00 00'  // Get MD5
  });
});

socket.on('debug_response', (response) => {
  if (response.success) {
    console.log('Debug data:', response.data);
  } else {
    console.error('Debug error:', response.error);
  }
});

socket.on('disconnect', () => {
  console.log('Disconnected');
});
```

## Client Implementation Example (Python)

### Installation
```bash
pip3 install python-socketio[client]
```

### Test Script
```python
#!/usr/bin/env python3
import socketio

# Get JWT token from /api/login first
TOKEN = "your_jwt_token_here"

sio = socketio.Client(ssl_verify=False)

@sio.event(namespace='/api/debug')
def connect():
    print("✓ Connected to WebSocket!")

@sio.event(namespace='/api/debug')
def connected(data):
    print(f"✓ Server confirmed: {data}")

@sio.event(namespace='/api/debug')
def debug_response(data):
    print(f"\n✓ Debug response:")
    print(f"  Success: {data.get('success')}")
    if data.get('success'):
        print(f"  Data: {data.get('data')}")
    else:
        print(f"  Error: {data.get('error')}")

@sio.event(namespace='/api/debug')
def disconnect():
    print("✗ Disconnected")

# Connect with token in auth dict (preferred method)
try:
    sio.connect(
        'https://localhost:8443',
        auth={'token': TOKEN},
        namespaces=['/api/debug'],
        transports=['websocket']
    )

    # Send debug command
    sio.emit('debug_command', {
        'command': '41 DE AD 00 00'  # DEBUG_INFO command
    }, namespace='/api/debug')

    sio.sleep(2)  # Wait for response
    sio.disconnect()

except Exception as e:
    print(f"Error: {e}")
```

**Note:** You can also pass the token via query string as a fallback:
```python
sio.connect(
    f'https://localhost:8443?token={TOKEN}',
    namespaces=['/api/debug'],
    transports=['websocket']
)
```

## Benefits

1. **Single TLS Handshake**: WebSocket connection is established once per debug session
2. **Persistent Connection**: No reconnection overhead during variable polling
3. **Bidirectional**: Efficient request-response pattern
4. **Secure**: JWT authentication + HTTPS/WSS encryption
5. **Compatible**: Uses same debug protocol format as existing Arduino/Modbus implementation

## Migration Notes

For OpenPLC Editor developers:

1. Replace TCP socket (port 502) connection with WebSocket connection
2. Obtain JWT token via REST API login before connecting
3. Use same hex string command format for debug commands
4. Parse responses in same format as Modbus implementation
5. Connection lifetime matches debug session (connect when debug starts, disconnect when debug stops)
