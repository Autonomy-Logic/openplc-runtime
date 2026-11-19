# HMI end-to-end test

Runs the Runtime v4 HMI (`docs/HMI.md`) end to end in Docker with a real program, a real HMI built
by the editor (screens as `hmi.wasm` for the browser, the data manifest `hmi_access.c` for the
device), the real web server and proxy, a Socket.IO viewer that speaks the browser's protocol, and
Chromium on `/hmi/` (`browser_e2e.mjs`).

```bash
docker build -t openplc-runtime:p13 .
HMI_E2E_CONTAINER=openplc-hmi-mine tests/hmi_e2e/run.sh openplc-runtime:p13 ~/path/to/openplc-editor
```

Inputs: the runtime image built from this tree, and an openplc-editor worktree with STruC++ installed
(`npm run setup:strucpp`), Electron installed (`node node_modules/electron/install.js` after an
`npm ci --ignore-scripts`) and the dev CLI bundle `openplc-cli.dev.js` built from it
(`npm run build:cli:dev`). The Python half needs `python-socketio[client]` in `venvs/test-env`;
`run.sh` installs it when it is missing. The browser half needs Node and a `playwright` package whose
Chromium is installed (`HMI_E2E_PLAYWRIGHT`, default the editor's).

Environment: `HMI_E2E_PORT` (host port, default 18543), `HMI_E2E_CONTAINER` (container name, default
`openplc-hmi-e2e`; a container of that name is replaced), `HMI_E2E_WORK` (work directory, kept; the
browser screenshots go to its `shots/`), `HMI_E2E_KEEP=1` (keep the container and the work directory).

## What it does

1. `project/` (program `Main` with `Run : BOOL` and `Level : REAL`, instance `instance0`, board
   OpenPLC Runtime v4) is compiled with `openplc-cli compile`, once as is and once with one more
   variable (the changed program of TC12).
2. `project/hmi/` holds two screens and their settings: `Overview` (a switch writing
   `instance0.Main.Run`, a lamp, a value display of `Level`, a Sign out button calling
   `HMI_SignOut()`, and a slider and a numeric input of `Setpoint`, both 0 to 100), granted to engineers and operators, and `Status` (the lamp and the value only),
   granted to every role. The editor's own build composes `hmi/` inside the program bundle. The
   changed program of TC12 is compiled without `project/hmi/`, so it carries no `hmi/`.
3. `e2e_hmi.py` starts the container (volume and container removed at the end), creates an admin,
   two operators and a viewer, uploads program + `hmi/` + a test board plugin (`vpp_plugin/`,
   `vpp_plugins.conf`), and checks:
   - the build log, the HMI host and board plugin log lines (TC13: both load); the device holds only
     `hmi_access.c` and `hmi_access.h` of the HMI;
   - `/hmi/` headers (CSP, nosniff, no-store, no-referrer); the sign-in page files public; `hmi.wasm`
     refused (401) without a token or with a bad one, a page load of it sent to `/hmi/`, and served as
     `application/wasm`, `no-store`, with a valid token; refused names, and a client file changed on the device refused against the seal, then served
     again once restored;
   - `/api/hmi`: a bad token refused; the operator's open gives `live` with its role; showing
     Overview sends its values, then only changes; a write of `Run` is applied (read back through the
     debugger, `/api/debug`, FC 0x43), comes back with the write count, `Level` keeps coming, the write
     is logged with the account;
   - refusals: a `Setpoint` of 30000 (the slider and the numeric input stop at 100; 100 is applied), a write of `Level`
     (Overview only reads it), a BOOL of 2, a write whose value is not
     bytes and a screen that does not exist are refused and logged, and none reaches the PLC; the
     viewer may not show Overview (then it is served nothing), may show Status, and its writes of
     `Run` are refused there; the operator's same write passes (negative control);
   - the browser: an operator signs in at `/hmi/`, taps the switch (the PLC's `Run` becomes TRUE) and
     receives a rising `Level`; the viewer's browser shows Status, its tap sends nothing, and a write
     and a show of Overview forged from that signed-in page are refused (the PLC's `Run` unchanged);
     the screen's Sign out button returns to the form and the token is refused afterwards; the sign-in
     page does not load `hmi.wasm`, a fetch of it answers 401 and a page load of it lands on the
     sign-in page, the signed-in client loads it once, and after sign-out it is refused again;
   - TC11: PLC stop gives `stopped` with the socket connected; start gives `live` and values again
     once the screen is shown, with the same socket;
   - TC27: 100 writes, each timed from the write to the values carrying it back; every one comes back
     within 300 ms; median, 95th percentile and maximum are printed;
   - TC15: 1,000 writes give exactly 1,000 applied writes in the host log, the last one winning;
   - TC12: an upload without `hmi/` keeps the HMI; after start the viewer gets `out_of_date`, no
     values and no writes follow, and a viewer connecting later gets `out_of_date` too.
