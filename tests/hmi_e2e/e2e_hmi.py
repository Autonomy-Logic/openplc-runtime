# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""End-to-end check of the Runtime v4 HMI (docs/HMI.md) against a running container.

Uploads a real program bundle with a real generated HMI unit (and a test board plugin), then
drives the viewer channel /api/hmi with a Socket.IO client: live status, display lists with
acknowledgements, a pointer click that writes a PLC variable, the sealed client files, a PLC
stop and start without reconnecting (TC11), an upload without hmi/ (TC12) and the HMI plugin
next to a board plugin (TC13).

Usage (normally through run.sh):
  e2e_hmi.py --image IMAGE --bundle DIR --hmi DIR --bundle2 DIR --vpp DIR [--port N] [--keep]
"""

import argparse
import io
import json
import os
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field

import requests
import socketio
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONTAINER = "openplc-hmi-e2e"
# Status byte of a successful debugger reply (core/src/plc_app/debug_handler.c).
DEBUG_SUCCESS = 0x7E
VOLUME = "openplc-hmi-e2e-data"
TLS = ssl.create_default_context()
TLS.check_hostname = False
TLS.verify_mode = ssl.CERT_NONE
# The switch is the screen's first element: Root column, padding 12, height 44.
SWITCH_X, SWITCH_Y = 40, 34
CSP = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; "
    "img-src 'self' data:; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)

ARGS = argparse.Namespace()
FAILED: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition else f'  -- {detail}'}", flush=True)
    if not condition:
        FAILED.append(label)
    return condition


def info(msg: str) -> None:
    print(f"      {msg}", flush=True)


# ---- container and REST --------------------------------------------------------------------


def base() -> str:
    return f"https://127.0.0.1:{ARGS.port}"


def api(method: str, path: str, token: str | None = None, body: object = None, **kw: object) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.request(
        method, f"{base()}/api{path}", headers=headers, json=body, verify=False, timeout=60, **kw
    )


def docker(*args: str, check_rc: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check_rc, capture_output=True, text=True)


def dexec(cmd: str) -> str:
    return docker("exec", CONTAINER, "bash", "-c", cmd, check_rc=False).stdout


def cleanup() -> None:
    docker("rm", "-f", CONTAINER, check_rc=False)
    docker("volume", "rm", "-f", VOLUME, check_rc=False)


def start_container() -> None:
    cleanup()
    docker(
        "run", "-d", "--name", CONTAINER, "--cap-add=sys_nice",
        "-p", f"{ARGS.port}:8443", "-v", f"{VOLUME}:/var/run/runtime", ARGS.image,
    )
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            if api("GET", "/version").status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError("the runtime did not answer /api/version")


def login(username: str, password: str) -> str:
    r = api("POST", "/login", body={"username": username, "password": password})
    assert r.status_code == 200, (username, r.status_code, r.text)
    return str(r.json()["access_token"])


def build_zip(parts: list[tuple[str, str]], extra: dict[str, bytes] | None = None) -> bytes:
    """A ZIP of each (directory, prefix in the ZIP) plus ``extra`` entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for directory, prefix in parts:
            for root, _dirs, files in os.walk(directory):
                for name in sorted(files):
                    path = os.path.join(root, name)
                    rel = os.path.relpath(path, directory).replace(os.sep, "/")
                    zf.write(path, f"{prefix}{rel}")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def upload(token: str, data: bytes, label: str) -> list[str]:
    """Uploads a program ZIP and waits for the build; returns the build log lines."""
    r = api("POST", "/upload-file", token, files={"file": ("program.zip", data, "application/zip")})
    body = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
    check(f"{label}: upload accepted", r.status_code == 200 and not body.get("UploadFileFail"), r.text)
    deadline = time.time() + 900
    status: dict[str, object] = {}
    while time.time() < deadline:
        status = api("GET", "/compilation-status", token).json()
        if status.get("status") in ("SUCCESS", "FAILED"):
            break
        time.sleep(2)
    logs = [str(line).rstrip() for line in status.get("logs", [])]  # type: ignore[union-attr]
    ok = check(f"{label}: compilation SUCCESS", status.get("status") == "SUCCESS", status.get("status"))
    for line in logs:
        if "HMI" in line or "VPP" in line or not ok:
            info(f"build| {line}")
    return logs


def plc_status(token: str) -> str:
    return str(api("GET", "/status", token).json().get("status", ""))


def plc(token: str, action: str, want: str) -> bool:
    api("GET", f"/{action}-plc", token)
    deadline = time.time() + 60
    st = ""
    while time.time() < deadline:
        st = plc_status(token)
        if want in st:
            return True
        time.sleep(0.5)
    info(f"status after {action}: {st}")
    return False


def runtime_log(token: str) -> str:
    return json.dumps(api("GET", "/runtime-logs", token).json())


def wait_log(token: str, needle: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in runtime_log(token) or needle in container_log():
            return True
        time.sleep(0.5)
    return False


def write_count(token: str) -> int:
    return len(log_lines(container_log(), "write by op1"))


def container_log() -> str:
    r = docker("logs", CONTAINER, check_rc=False)
    return r.stdout + r.stderr


def log_lines(text: str, *needles: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\\n", "\n").splitlines():
        if any(n in line for n in needles):
            out.append(line.strip()[:300])
    return out


# ---- debugger -----------------------------------------------------------------------------


def read_vars(token: str) -> tuple[int, float] | None:
    """Run and Level through the debugger (/api/debug, DEBUG_GET of array 0, elements 0-1)."""
    sio = socketio.Client(ssl_verify=False)
    reply: dict[str, object] = {}
    got = threading.Event()

    @sio.on("debug_response", namespace="/api/debug")
    def _resp(data: dict[str, object]) -> None:
        reply.update(data)
        got.set()

    try:
        sio.connect(base(), namespaces=["/api/debug"], auth={"token": token}, transports=["websocket"])
        sio.emit("debug_command", {"command": "43 00 00 00 00 01"}, namespace="/api/debug")
        got.wait(5)
    finally:
        sio.disconnect()
    if not reply.get("success"):
        info(f"debugger: {reply}")
        return None
    raw = bytes.fromhex(str(reply["data"]).replace(" ", ""))
    if len(raw) < 15 or raw[0] != 0x43 or raw[1] != DEBUG_SUCCESS:
        info(f"debugger: unexpected reply {raw.hex()}")
        return None
    data = raw[10:]
    return data[0], struct.unpack("<f", data[1:5])[0]


# ---- HMI viewer ---------------------------------------------------------------------------


@dataclass
class Viewer:
    """One Socket.IO viewer on /api/hmi that records statuses and display lists."""

    token: str
    statuses: list[tuple[float, str]] = field(default_factory=list)
    frames: list[tuple[float, int]] = field(default_factory=list)
    connects: int = 0
    disconnects: int = 0
    n: int = 0
    ack: bool = True

    def __post_init__(self) -> None:
        self.sio = socketio.Client(ssl_verify=False, reconnection=False)
        ns = "/api/hmi"

        @self.sio.on("connect", namespace=ns)
        def _connect() -> None:
            self.connects += 1

        @self.sio.on("disconnect", namespace=ns)
        def _disconnect(*_a: object) -> None:
            self.disconnects += 1

        @self.sio.on("hmi_status", namespace=ns)
        def _status(data: dict[str, str]) -> None:
            self.statuses.append((time.monotonic(), str(data.get("state"))))

        @self.sio.on("dl", namespace=ns)
        def _dl(data: bytes) -> object:
            self.frames.append((time.monotonic(), len(data) if isinstance(data, (bytes, bytearray)) else -1))
            return True if self.ack else None

    def connect(self) -> None:
        self.sio.connect(base(), namespaces=["/api/hmi"], auth={"token": self.token}, transports=["websocket"])

    def send(self, event: dict[str, object]) -> None:
        event["n"] = self.n
        self.n += 1
        self.sio.emit("in", event, namespace="/api/hmi")

    def viewport(self, w: int = 800, h: int = 480, d: float = 1) -> None:
        self.send({"t": "v", "w": w, "h": h, "d": d})

    def click(self, x: float, y: float) -> None:
        self.send({"t": "p", "k": 0, "x": x, "y": y})
        time.sleep(0.15)
        self.send({"t": "p", "k": 2, "x": x, "y": y})

    def wait_status(self, state: str, since: float, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(t >= since and s == state for t, s in self.statuses):
                return True
            time.sleep(0.1)
        return False

    def frames_since(self, since: float) -> int:
        return sum(1 for t, _ in self.frames if t >= since)

    def last_status(self) -> str | None:
        return self.statuses[-1][1] if self.statuses else None


# ---- the test -----------------------------------------------------------------------------


def client_files(admin: str) -> None:
    del admin
    index = open(os.path.join(ARGS.hmi, "hmi", "client", "index.html"), "rb").read()
    r = requests.get(f"{base()}/hmi/", verify=False, timeout=30)
    check("GET /hmi/ serves the sealed index.html", r.status_code == 200 and r.content == index, r.status_code)
    for name, value in (
        ("Content-Security-Policy", CSP),
        ("X-Content-Type-Options", "nosniff"),
        ("Cache-Control", "no-store"),
        ("Referrer-Policy", "no-referrer"),
    ):
        check(f"/hmi/ header {name}", r.headers.get(name) == value, r.headers.get(name))
    check("/hmi/ content type html", r.headers.get("Content-Type", "").startswith("text/html"), r.headers)
    r = requests.get(f"{base()}/hmi", verify=False, timeout=30, allow_redirects=False)
    check("GET /hmi redirects to /hmi/", r.status_code == 302 and r.headers.get("Location", "").endswith("/hmi/"), r.status_code)
    for bad in ("hmi.seal", "libhmi_plugin.so", "..%2Fhmi.seal", "x.php"):
        r = requests.get(f"{base()}/hmi/{bad}", verify=False, timeout=30)
        check(f"GET /hmi/{bad} refused", r.status_code == 404, r.status_code)

    client = "/workdir/build/hmi/client/index.html"
    dexec(f"cp {client} /tmp/index.html.orig && printf '<script>evil()</script>' >> {client}")
    r = requests.get(f"{base()}/hmi/index.html", verify=False, timeout=30)
    check("tampered client file refused (404)", r.status_code == 404, r.status_code)
    lines = log_lines(container_log(), "does not match the seal")
    check("tampered client file logged", bool(lines), "no log line")
    for line in lines[-1:]:
        info(f"log| {line}")
    dexec(f"cp /tmp/index.html.orig {client}")
    r = requests.get(f"{base()}/hmi/", verify=False, timeout=30)
    check("restored client file served again", r.status_code == 200 and r.content == index, r.status_code)


def main() -> None:
    start_container()
    r = api("POST", "/create-user", body={"username": "admin", "password": "admin-pass"})
    check("bootstrap admin", r.status_code == 201, r.text)
    admin = login("admin", "admin-pass")
    r = api("POST", "/create-user", admin, {"username": "op1", "password": "op1-pass", "role": "operator"})
    check("create operator op1", r.status_code == 201, r.text)
    operator = login("op1", "op1-pass")

    r = requests.get(f"{base()}/hmi/", verify=False, timeout=30)
    check("before any HMI: /hmi/ shows the no-HMI page (503)", r.status_code == 503 and b"No HMI installed" in r.content, r.status_code)

    vpp_conf = b"e2evpp,./build/vpp/libe2evpp_plugin.so,1,1\n"
    zip1 = build_zip(
        [(ARGS.bundle, ""), (ARGS.hmi, ""), (ARGS.vpp, "vpp_plugin/")],
        {"vpp_plugins.conf": vpp_conf},
    )
    logs = upload(admin, zip1, "upload with hmi/ and a board plugin")
    check("build log: HMI installed from upload", any("HMI: installed from upload" in l for l in logs))
    check("build log: HMI plugin built and sealed", any("HMI plugin built and sealed" in l for l in logs))
    check("build log: VPP plugin compiled", any("VPP plugin compiled successfully" in l for l in logs))
    check("start PLC", plc(admin, "start", "RUNNING"))
    info(f"build/hmi: {dexec('ls -la /workdir/build/hmi /workdir/build/hmi/client; cat /workdir/build/hmi/hmi.seal').strip()}")

    # TC13: the HMI plugin and the board plugin load together.
    check("TC13 board plugin init", wait_log(admin, "E2E-VPP init", 15))
    check("TC13 board plugin start_loop", wait_log(admin, "E2E-VPP start_loop", 15))
    check("TC13 HMI host serving next to it", wait_log(admin, "serving viewers on", 15))
    check("HMI host: screens match the PLC program", wait_log(admin, "screens match the PLC program", 15))
    for line in log_lines(runtime_log(admin) + container_log(), "E2E-VPP", "[HMI]", "HMI plugin", "screens match", "serving viewers"):
        info(f"log| {line}")

    client_files(admin)

    # Viewer channel.
    bad = socketio.Client(ssl_verify=False, reconnection=False)
    try:
        bad.connect(base(), namespaces=["/api/hmi"], auth={"token": "nope"}, transports=["websocket"])
        refused = False
    except socketio.exceptions.ConnectionError:
        refused = True
    finally:
        bad.disconnect()
    check("/api/hmi refuses a bad token", refused)

    v = Viewer(operator)
    v.connect()
    check("operator connects to /api/hmi", v.sio.connected)
    sid = v.sio.get_sid("/api/hmi")
    t0 = time.monotonic()
    v.viewport(800, 480, 1)
    check("hmi_status live after the first viewport", v.wait_status("live", t0), v.statuses)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not v.frames:
        time.sleep(0.1)
    check("display list (dl) received", bool(v.frames) and v.frames[0][1] > 0, v.frames[:3])
    info(f"first dl: {v.frames[0][1] if v.frames else 0} bytes")
    check("operator viewer opened (host log)", wait_log(admin, "opened by op1 (", 10))

    before = read_vars(admin)
    check("debugger reads Run = FALSE before the click", before is not None and before[0] == 0, before)
    quiet = time.monotonic()
    time.sleep(2.0)
    idle_frames = v.frames_since(quiet)
    info(f"frames in 2 s with Run FALSE (static screen): {idle_frames}")

    v.click(SWITCH_X, SWITCH_Y)
    time.sleep(1.0)
    after = read_vars(admin)
    time.sleep(1.0)
    later = read_vars(admin)
    info(f"debugger Run/Level: before {before}, after {after}, 1 s later {later}")
    check("click on the switch wrote Run = TRUE (debugger)", after is not None and after[0] == 1, after)
    check("Level rises while Run is TRUE", after is not None and later is not None and later[1] > after[1], (after, later))
    busy = time.monotonic()
    time.sleep(2.0)
    live_frames = v.frames_since(busy)
    check("frames keep coming while Level changes (acks honoured)", live_frames >= 5, (idle_frames, live_frames))
    info(f"frames in 2 s with Level changing: {live_frames}")
    check("write logged with the account", wait_log(admin, "write by op1:", 10))
    for line in log_lines(runtime_log(admin) + container_log(), "write by op1"):
        info(f"log| {line}")

    # TC11: stop and start keep the socket.
    t1 = time.monotonic()
    check("stop PLC", plc(admin, "stop", "STOPPED"))
    check("TC11 hmi_status stopped on PLC stop", v.wait_status("stopped", t1), v.statuses)
    time.sleep(2)
    check("TC11 socket stays connected while stopped", v.sio.connected and v.disconnects == 0)
    t2 = time.monotonic()
    check("start PLC", plc(admin, "start", "RUNNING"))
    check("TC11 hmi_status live again after start", v.wait_status("live", t2, 20), v.statuses)
    time.sleep(1.5)
    check("TC11 no reconnect (same sid, one connect)", v.sio.get_sid("/api/hmi") == sid and v.connects == 1 and v.disconnects == 0, (v.connects, v.disconnects))
    check("TC11 frames resume without a new viewport", v.frames_since(t2) > 0)

    # A second click toggles Run from whatever the restarted program holds (Run is not retained).
    current = read_vars(admin)
    info(f"debugger Run/Level after the restart: {current}")
    v.click(SWITCH_X, SWITCH_Y)
    time.sleep(1.0)
    toggled = read_vars(admin)
    check(
        "second click toggled Run (debugger)",
        current is not None and toggled is not None and toggled[0] == 1 - current[0],
        (current, toggled),
    )

    # TC12: an upload without hmi/ keeps the HMI, which is then out of date.
    zip2 = build_zip([(ARGS.bundle2, "")])
    t3 = time.monotonic()
    logs = upload(admin, zip2, "TC12 upload of a changed program without hmi/")
    check("TC12 build log: kept the installed HMI", any("kept the installed HMI" in l for l in logs))
    check("TC12 start PLC", plc(admin, "start", "RUNNING"))
    check("TC12 viewer gets out_of_date", v.wait_status("out_of_date", t3, 30), v.statuses)
    check("TC12 host log: screens out of date", wait_log(admin, "screens out of date", 10))
    for line in log_lines(runtime_log(admin) + container_log(), "out of date"):
        info(f"log| {line}")
    writes_before = write_count(admin)
    t4 = time.monotonic()
    v.viewport(800, 480, 1)
    v.click(SWITCH_X, SWITCH_Y)
    time.sleep(3)
    check("TC12 no frames while out of date (viewport and click sent)", v.frames_since(t4) == 0, v.frames_since(t4))
    check("TC12 the click did not write", write_count(admin) == writes_before, (writes_before, write_count(admin)))
    late = Viewer(operator)
    t5 = time.monotonic()
    late.connect()
    check("TC12 a viewer connecting later gets out_of_date", late.wait_status("out_of_date", t5, 10), late.statuses)
    late.sio.disconnect()
    check("TC12 the first viewer is still connected", v.sio.connected and v.disconnects == 0)
    v.sio.disconnect()
    check("viewer close logged by the host", wait_log(admin, "viewer", 5))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="openplc-runtime:p9")
    parser.add_argument("--bundle", required=True, help="program bundle src/ of the first build")
    parser.add_argument("--hmi", required=True, help="directory holding hmi/")
    parser.add_argument("--bundle2", required=True, help="program bundle src/ of the changed program")
    parser.add_argument("--vpp", required=True, help="board plugin source directory (Makefile)")
    parser.add_argument("--port", type=int, default=18543)
    parser.add_argument("--keep", action="store_true", help="leave the container running")
    ARGS = parser.parse_args()
    try:
        main()
    except Exception as e:  # noqa: BLE001
        check("unexpected error", False, repr(e))
    finally:
        if FAILED:
            print("---- container log (tail) ----")
            print("\n".join(container_log().splitlines()[-80:]))
        if not ARGS.keep:
            cleanup()
    print("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1 if FAILED else 0)
