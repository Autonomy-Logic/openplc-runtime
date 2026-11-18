# HMI on Runtime v4

Operator screens designed in the OpenPLC Editor run on the device as a dedicated native plugin,
`build/hmi/libhmi_plugin.so`, loaded into `plc_main` beside any board (VPP) plugin. Browsers reach
them through the runtime's own HTTPS server: the client at `/hmi/` and the viewer channel on the
Socket.IO namespace `/api/hmi`, port 8443, no extra port.

```
browser ──HTTPS/WSS──> web server (webserver/hmi_proxy.py) ──Unix socket──> HMI plugin in plc_main
                       /hmi/ assets, /api/hmi namespace        hmi.socket     host + core + unit
                                                                               │ debug_read / debug_write
                                                                               ▼
                                                                             PLC program .so
```

## Upload artefact

A runtime that accepts it says so with `"hmi": true` at `GET /api/capabilities` (unauthenticated);
older runtimes refuse the client's `.js` file, and with it the upload. A Runtime v4 upload may carry an
`hmi/` folder next to the program sources:

| Path in the upload | Content |
| --- | --- |
| `hmi/manifest.json` | `{"format": 1}`; anything else is refused |
| `hmi/src/` | The HMI core (`*.c`, `*.h`) and the generated unit (`*.cpp`, `*.hpp`, `*.h`, including `hmi_unit.h`) |
| `hmi/client/` | The browser client. Only `.html`, `.js`, `.css`, `.wasm`, `.txt` and `.ttf` files (extensions in lower case), names matching `^[A-Za-z0-9._-]{1,64}$`, no subfolders |

- `.js`, `.wasm` and `.html` stay refused anywhere else in an upload.
- An upload with `hmi/` replaces the previous HMI. An upload without `hmi/` keeps it; its MD5 then no
  longer matches the program and it reports "out of date" (TC12).
- An `hmi/manifest.json` with `{"format": 1, "remove": true}` and nothing else removes the HMI.

## Build and seal

`scripts/compile.sh` builds the plugin after the program when `core/generated/hmi/` exists:

- sources: the runtime's host (`core/src/drivers/plugins/native/hmi/*.c`) plus `hmi/src/**/*.c` and
  `hmi/src/**/*.cpp`, against the STruC++ runtime headers the program build uses;
- flags: `-Wall -Wextra`, position independent, C11 / C++17;
- output: `build/hmi/libhmi_plugin.so`; `hmi/client/*` is copied to `build/hmi/client/`. The build
  goes to `build/hmi.new` and replaces `build/hmi` only when it succeeds;
- a failed build (program or HMI) changes nothing on the device: the PLC is not stopped, the running
  program's `.so` is not replaced and the installed HMI stays;
- seal: `build/hmi/hmi.seal`, one line per file, `<sha256 hex>  <path relative to build/hmi>`, covering
  the plugin and every client file. `plc_main` checks the plugin's line before `dlopen` and loads only
  `libhmi_plugin.so` from `build/hmi`; the web server checks a client file's line before serving it. A
  missing or mismatching seal refuses the file.

The plugin is registered by `plc_main` itself (not through `plugins.conf` or `vpp_plugins.conf`)
whenever `build/hmi/libhmi_plugin.so` exists, under the name `hmi`.

## Plugin arguments

`plugin_runtime_args_t` gains `char program_md5[33]`: the running program's MD5 (the value the debugger
verifies), NUL-terminated, empty when no program is loaded. The Python ctypes mirror
(`core/src/drivers/plugins/python/shared/plugin_runtime_args.py`) follows.

The host compares it with the unit's `hmi_plc_md5`. On a mismatch, or when either is empty, the HMI is
**out of date**: it opens no viewer, makes no debug read or write, and answers every OPEN with
STATUS out_of_date.

## Unit interface

The generated unit reaches PLC variables only through the host (`hmi_unit.h`):

```c
typedef struct {
    /* Copies the tag's current value into dest in its buffer format (hmi_tag_size bytes); false when it cannot be read. */
    bool (*read)(void *ctx, int tag, void *dest);
    /* Applies a screen's write of the tag; src holds the new value in its buffer format. */
    void (*write)(void *ctx, int tag, const void *src);
    void *ctx;
} hmi_tag_access_t;
```

Buffer format: the tag's IEC width, little endian; a STRING is `max_len + 1` bytes, NUL terminated.
The unit reads a value at the moment the screen code uses it; nothing is copied per frame. `hmi_tags[]`
gives each tag's debug address (`arr`, `elem`), type and `max_len`. The host converts between the
buffer format and the STruC++ debug format of `debug_read` / `debug_write` (soft write, respects forcing).

## Host

- Socket: `/run/runtime/hmi/hmi.socket`, in a directory `start_loop` creates (or takes over when it is
  a real directory of the same user, not a symlink) with mode 0700; the socket is removed in
  `stop_loop`. One connection at a time, from the web server, which connects only when the directory
  is owner-only and both belong to its own user.
- Viewers: one `hmi_session_t` and one unit instance (`hmi_unit_create`) per viewer, with
  `hmi_unit_set_role` set from OPEN before the first frame; the unit enforces the screen grid. BUSY
  answers an allocation failure, an account name that is not printable UTF-8 (that viewer only; the
  connection stays) and an input queue overflow; there is no viewer limit.
- Frames: a frame thread at the unit's `hmi_frame_ms` renders each viewer and keeps only its latest
  display list; a writer sends pending frames without blocking, so a slow web server or viewer never
  delays another viewer or the PLC. A frame is sent only when its hash changed.
- Input: applied in arrival order per viewer, from a queue of 128 events; pointer moves are coalesced
  when the queue is full. Each frame applies at most one press or release; more presses, releases and
  keys than the queue holds close the viewer with BUSY.
- Writes: each write is logged with the viewer's account, tag path and new value.
- Sign-out: a frame whose screen code called `HMI_SignOut()` (`hmi_frame_sign_out`) sends SIGN_OUT for its
  viewer once and logs it with the account; the web server ends the session.
- Faults: `hmi_unit_frame` returns false when the viewer's screen code raised an IEC fault (null
  reference, array bounds); the unit catches it, so a fault never reaches `plc_main`. The host logs
  it, closes that viewer and sends FAULT; the PLC and other viewers are unaffected.
- Logs: the number of viewers on every open and close.
- Threads run at the default priority (`SCHED_OTHER`, nice 0); PLC tasks keep `SCHED_FIFO`.
- Stop: `stop_loop` waits up to 5 seconds for the frame in progress. Screen code that never returns
  would keep running code `plc_main` is about to unload, so past that `plc_main` logs it and exits with
  status 43; the web server restarts it as after any crash.

## Wire protocol (web server <-> host)

Every message: `u32 length (LE) | u8 type | payload`, `length` counting type and payload, at most
4 MiB. Integers little endian. A malformed message closes the connection.

Web server to host:

| Type | Name | Payload |
| --- | --- | --- |
| 0x01 | OPEN | `u32 viewer, u8 role (0 admin, 1 engineer, 2 operator, 3 viewer), u16 width, u16 height, u16 density x100, u8 name length, name (UTF-8, at most 64 bytes; the web server replaces control characters with '?')` |
| 0x02 | CLOSE | `u32 viewer` |
| 0x03 | VIEWPORT | `u32 viewer, u16 width, u16 height, u16 density x100` |
| 0x04 | POINTER | `u32 viewer, u8 kind (0 down, 1 move, 2 up), i32 x x100, i32 y x100` (CSS pixels) |
| 0x05 | KEY | `u32 viewer, u32 code` |

Host to web server:

| Type | Name | Payload |
| --- | --- | --- |
| 0x81 | FRAME | `u32 viewer, display list bytes` |
| 0x82 | STATUS | `u8 state (0 live, 1 out_of_date)` |
| 0x83 | OPENED | `u32 viewer` |
| 0x84 | BUSY | `u32 viewer` |
| 0x85 | FAULT | `u32 viewer` (its screen code raised an IEC fault; the viewer was closed) |
| 0x86 | SIGN_OUT | `u32 viewer` (its screen code called `HMI_SignOut`; sent once per viewer, which stays open) |

Viewport sizes and pointer positions are CSS pixels, and one HMI logical unit is one CSS pixel, as in
the editor's preview; `density` (the device pixel ratio) only sets the client's raster resolution.
`width`/`height` 1-8192, `density` 25-800.

## Web server (`webserver/hmi_proxy.py`)

- `GET /hmi/` and `/hmi/<file>` serve `build/hmi/client/`, names whitelisted, each file checked against
  the seal, with `Content-Security-Policy: default-src 'self'; script-src 'self' 'wasm-unsafe-eval';
  connect-src 'self'; img-src 'self' data:; style-src 'self'; frame-ancestors 'none'; base-uri 'none';
  form-action 'self'`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` and
  `Referrer-Policy: no-referrer`. Unauthenticated: the page shows the sign-in form.
- Namespace `/api/hmi`: the access token is checked at connect (`auth.token`) with the same rules as
  every endpoint (signature, expiry, revocation, session, account, role in `webserver/access.py`).
  While connected it is re-checked every 5 seconds and on each input event for revocation, session,
  account and role, as the debugger does; expiry is enforced at connect only, so a renewal does not
  interrupt a viewer. A refused token or a role change disconnects.
- Client event `in`, rebuilt from whitelisted, range-checked fields (booleans, NaN and infinity refused):
  `{t:"v",w,h,d,n}` viewport (`w`/`h` CSS pixels 1-8192, `d` the device pixel ratio 0.25-8),
  `{t:"p",k,x,y,n}` pointer (`k` 0 down, 1 move, 2 up; `x`/`y` CSS pixels within ±100000),
  `{t:"k",c,n}` key (`c` a Unicode code point). `n` is the client's sequence number (0 to 2^53);
  events are forwarded in `n` order per viewer, at most 64 held; an invalid event still uses its
  number. The first viewport opens the viewer; pointer and key events before it, or while the host is
  down, are dropped.
- Server events: `dl` (display list bytes) is sent with an acknowledgement: the client must call the
  ack when it has taken the frame, and until then only the newest frame is kept for that viewer (after
  1 second without an ack the next frame is sent anyway). `hmi_status` carries `state`: `live` (sent on
  OPENED), `stopped` (the HMI is installed but the PLC is stopped: viewers stay connected and are
  reopened with their last viewport on start), `out_of_date` (to every viewer, and to viewers that
  connect later), `busy` (the host refused this viewer, see Host; its next viewport retries), `fault` (its
  screen code raised an IEC fault and the viewer was closed; its next viewport reopens it) or
  `unavailable` (no HMI installed). `hmi_signed_out` follows a SIGN_OUT: the web server ends that
  viewer's session as `/api/logout` does (its token and its renewals are refused from then on), tells
  the browser and disconnects it. A token that no longer verifies (the signing key changed) has its
  session ended by its id. If the database cannot end the session, the browser is still signed out
  and disconnected, and the error is logged. A message the web server cannot handle drops the host
  connection, which is then opened again; it never stops the HMI for good.

## Browser client (`hmi/client/`)

The editor ships the client with every HMI; the runtime serves it as it is. It is a page with no inline
script or style (the CSP above), `hmi-client.js`, socket.io-client 4.8.1 and `hmi_client.wasm`: the
HMI core's rasterizer with the unit's font table, so a browser draws a display list to the same pixels
as the editor's preview. Licence texts ship beside it (`fonts-LICENSE.txt`, `socket.io-LICENSE.txt`).

- Sign in: the page's form posts to `/api/login`; a 401 shows "Wrong user name or password.", a 429
  the wait from `Retry-After`. The token is kept in the browser's `localStorage`, so a reload or a
  browser restart on the same device stays signed in while the token is valid.
- Renewal: `/api/renew` three minutes before the 15-minute expiry, retried every 30 seconds while the
  device cannot be reached. A token that expires first (the display was unreachable for longer than
  its lifetime), a refused renewal or a disconnect whose `/api/whoami` answers 401 return to the form.
- Viewer: `/api/hmi` with `auth.token` read at each (re)connection; events are numbered from 0 per
  connection. The first viewport is the window's size and device pixel ratio, sent again on resize.
  Pointer moves are sent at most once per animation frame while pressed; keys carry printable code
  points plus Enter, Backspace, Escape and Tab.
- Notices: `stopped` "PLC stopped", `out_of_date` "HMI out of date, rebuild the project", `busy` "Device
  busy, try later" and `fault` (both asked again after 5 seconds), `unavailable` "No HMI installed",
  and "Device unreachable, reconnecting" while the socket is down.
- Sign out: only through the project's screens. A screen's button calls `HMI_SignOut()` (the designer's
  "Sign out" action); the device ends the session and the client returns to the form on
  `hmi_signed_out`. The client draws nothing over the screens; a session also ends when its token
  expires unrenewed, is revoked, or the account changes.
- `HMI_Lock()` and `HMI_Calibrate()` do nothing here. The PIN entry, the startup screen and the touch
  calibration belong to a board's own panel; a browser viewer signs in with its account and points
  with a calibrated device.
- While a notice shows, the last frame stays visible, dimmed and grey, and takes no input.

## Known limits

- STRING tags are read and written through the debug interface, which carries a string in a
  126-character window: a STRING declared longer than 126 is truncated when read and cut when written
  (the cut is logged). Accepted for this release (developer decision 2026-11-16).
