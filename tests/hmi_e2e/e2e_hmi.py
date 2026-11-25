# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""End-to-end check of the Runtime v4 HMI (docs/HMI.md) against a running container.

Uploads a real program bundle with a real HMI built by the editor (and a test board plugin), then
drives the value channel /api/hmi with a Socket.IO client as the browser does (open, show a
screen, write tags): values of the screen shown, writes applied and refused by role and screen,
the sealed client files, a PLC stop and start without reconnecting (TC11), an upload without hmi/
(TC12) and the HMI plugin next to a board plugin (TC13). The browser itself (Chromium, through
browser_e2e.mjs) signs in, taps, is refused and signs out, checked on the PLC side here.

Usage (normally through run.sh):
  e2e_hmi.py --image IMAGE --bundle DIR --hmi DIR --bundle2 DIR --vpp DIR --playwright DIR
             [--port N] [--container NAME] [--shots DIR] [--keep]
"""

import argparse
import io
import json
import os
import re
import ssl
import struct
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field

import requests
import socketio
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Status byte of a successful debugger reply (core/src/plc_app/debug_handler.c).
DEBUG_SUCCESS = 0x7E
TLS = ssl.create_default_context()
TLS.check_hostname = False
TLS.verify_mode = ssl.CERT_NONE
# A write's value is back in the viewer's values within this time (frame period 100 ms).
WRITE_ROUND_TRIP_MS = 300
NO_PAGE = 0xFFFF
CSP = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; "
    "img-src 'self' data:; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)

ARGS = argparse.Namespace()
FAILED: list[str] = []


def read_file(*parts: str) -> bytes:
    with open(os.path.join(*parts), "rb") as f:
        return f.read()


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
    return docker("exec", ARGS.container, "bash", "-c", cmd, check_rc=False).stdout


def volume() -> str:
    return f"{ARGS.container}-data"


def cleanup() -> None:
    docker("rm", "-f", ARGS.container, check_rc=False)
    docker("volume", "rm", "-f", volume(), check_rc=False)


def start_container() -> None:
    cleanup()
    docker(
        "run", "-d", "--name", ARGS.container, "--cap-add=sys_nice",
        "-p", f"{ARGS.port}:8443", "-v", f"{volume()}:/var/run/runtime", ARGS.image,
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
    """Writes by op1 the host applied; refused and failed writes are logged too and not counted."""
    return sum(
        1
        for line in log_lines(container_log(), "write by op1:")
        if " refused: " not in line and " failed" not in line
    )


def container_log() -> str:
    r = docker("logs", ARGS.container, check_rc=False)
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


# ---- HMI manifest ------------------------------------------------------------------------


@dataclass(frozen=True)
class Manifest:
    """Tag ids by path suffix and page numbers by screen name, read from the built hmi_access.c."""

    tags: dict[str, int]
    pages: dict[str, int]


def read_manifest() -> Manifest:
    text = read_file(ARGS.hmi, "hmi", "src", "hmi_access.c").decode("utf-8")
    paths = re.findall(r'\{\d+u, \d+u, \d+, \d+, "([^"]+)"\}', text)
    tags = {path.rsplit(".", 1)[-1]: i for i, path in enumerate(paths)}
    names = re.findall(r"\{reads_\d+, \d+, writes_\d+, \d+, \d+\}, /\* (\w+) \*/", text)
    return Manifest(tags, {name: i for i, name in enumerate(names)})


# ---- HMI viewer ---------------------------------------------------------------------------


@dataclass
class Values:
    at: float
    page: int
    writes: int
    records: dict[int, bytes]


def unpack_values(data: bytes) -> Values:
    page, writes, count = struct.unpack_from("<HIH", data)
    records: dict[int, bytes] = {}
    off = 8
    for _ in range(count):
        tag, size = struct.unpack_from("<HH", data, off)
        records[tag] = bytes(data[off + 4 : off + 4 + size])
        off += 4 + size
    return Values(time.monotonic(), page, writes, records)


@dataclass
class Viewer:
    """One Socket.IO viewer on /api/hmi as the browser client drives it: open, show, write."""

    token: str
    statuses: list[tuple[float, dict[str, object]]] = field(default_factory=list)
    values: list[Values] = field(default_factory=list)
    refused: list[tuple[float, dict[str, object]]] = field(default_factory=list)
    connects: int = 0
    disconnects: int = 0
    n: int = 0
    writes: int = 0
    build: str | None = None

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
        def _status(data: dict[str, object]) -> None:
            if data.get("state") == "live" and isinstance(data.get("build"), str):
                self.build = str(data["build"])
            self.statuses.append((time.monotonic(), dict(data)))

        @self.sio.on("hmi_refused", namespace=ns)
        def _refused(data: dict[str, object]) -> None:
            self.refused.append((time.monotonic(), dict(data)))

        @self.sio.on("hmi_values", namespace=ns)
        def _values(data: bytes) -> object:
            if isinstance(data, (bytes, bytearray)):
                self.values.append(unpack_values(bytes(data)))
            return True

    def connect(self) -> None:
        self.sio.connect(base(), namespaces=["/api/hmi"], auth={"token": self.token}, transports=["websocket"])

    def send(self, event: dict[str, object]) -> None:
        """Numbers the event; a show or write names the HMI build of the last live status."""
        if event.get("t") != "o" and "h" not in event:
            event["h"] = self.build
        event["n"] = self.n
        self.n += 1
        self.sio.emit("in", event, namespace="/api/hmi")

    def open(self) -> None:
        self.writes = 0
        self.send({"t": "o"})

    def show(self, page: int) -> None:
        self.send({"t": "s", "p": page})

    def write(self, tag: int, value: bytes) -> int:
        """Sends a write; returns its index in the viewer's write count."""
        self.writes += 1
        self.send({"t": "w", "g": tag, "v": value})
        return self.writes

    def wait_status(self, state: str, since: float, timeout: float = 15.0) -> dict[str, object] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for t, s in self.statuses:
                if t >= since and s.get("state") == state:
                    return s
            time.sleep(0.05)
        return None

    def wait_values(self, pred: Callable[[Values], bool], since: float, timeout: float = 10.0) -> Values | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for v in list(self.values):
                if v.at >= since and pred(v):
                    return v
            fail = self.failure(since)
            if fail:
                info(f"viewer failure while waiting for values: {fail}")
                return None
            time.sleep(0.005)
        return None

    def wait_refused(self, what: str, which: int, since: float, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(t >= since and r == {"what": what, "id": which} for t, r in self.refused):
                return True
            time.sleep(0.05)
        return False

    def failure(self, since: float) -> str | None:
        """A status other than live since `since`: what makes a wait for values pointless."""
        for t, s in self.statuses:
            if t >= since and s.get("state") not in ("live", None):
                return str(s.get("state"))
        return None

    def values_since(self, since: float) -> list[Values]:
        return [v for v in list(self.values) if v.at >= since]


def latest(viewer: Viewer, tag: int) -> bytes | None:
    for v in reversed(list(viewer.values)):
        if tag in v.records:
            return v.records[tag]
    return None


def browser(scenario: str, **opts: object) -> dict[str, object]:
    """Runs one browser_e2e.mjs scenario; its JSON result, with an error when it failed."""
    here = os.path.dirname(os.path.abspath(__file__))
    options = {"url": base(), "shots": ARGS.shots, "playwright": ARGS.playwright, **opts}
    r = subprocess.run(
        ["node", os.path.join(here, "browser_e2e.mjs"), scenario, json.dumps(options)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    lines = [line for line in r.stdout.splitlines() if line.startswith("{")]
    result: dict[str, object] = json.loads(lines[-1]) if lines else {"error": r.stderr[-2000:]}
    if r.returncode != 0 and "error" not in result:
        result["error"] = r.stderr[-2000:]
    return result


# ---- the test -----------------------------------------------------------------------------


def client_files(admin: str) -> None:
    index = read_file(ARGS.hmi, "hmi", "client", "index.html")
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
    wasm = read_file(ARGS.hmi, "hmi", "client", "hmi.wasm")
    for name in ("hmi-client.js", "hmi.css", "socket.io.min.js", "socket.io-LICENSE.txt", "fonts-LICENSE.txt"):
        r = requests.get(f"{base()}/hmi/{name}", verify=False, timeout=30)
        check(f"GET /hmi/{name} needs no account (sign-in page file)", r.status_code == 200, r.status_code)
    for label, headers in (("no token", {}), ("a bad token", {"Authorization": "Bearer nope"})):
        r = requests.get(f"{base()}/hmi/hmi.wasm", headers=headers, verify=False, timeout=30)
        check(
            f"GET /hmi/hmi.wasm with {label} is refused (401), the module not sent",
            r.status_code == 401 and wasm not in r.content,
            r.status_code,
        )
    r = requests.get(
        f"{base()}/hmi/hmi.wasm",
        headers={"Accept": "text/html", "Sec-Fetch-Mode": "navigate"},
        verify=False,
        timeout=30,
        allow_redirects=False,
    )
    check(
        "a page load of /hmi/hmi.wasm without a token goes to the sign-in page",
        r.status_code == 302 and r.headers.get("Location", "").endswith("/hmi/"),
        r.status_code,
    )
    r = requests.get(f"{base()}/hmi/hmi.wasm", headers={"Authorization": f"Bearer {admin}"}, verify=False, timeout=30)
    check(
        "GET /hmi/hmi.wasm with a valid token serves the module as application/wasm, not cached",
        r.status_code == 200
        and r.content == wasm
        and r.headers.get("Content-Type") == "application/wasm"
        and r.headers.get("Cache-Control") == "no-store",
        (r.status_code, r.headers.get("Content-Type"), r.headers.get("Cache-Control")),
    )
    info(f"hmi.wasm: {len(wasm)} bytes")
    r = requests.get(f"{base()}/hmi", verify=False, timeout=30, allow_redirects=False)
    check("GET /hmi redirects to /hmi/", r.status_code == 302 and r.headers.get("Location", "").endswith("/hmi/"), r.status_code)
    for bad in ("hmi.seal", "libhmi_plugin.so", "..%2Fhmi.seal", "x.php"):
        r = requests.get(f"{base()}/hmi/{bad}", verify=False, timeout=30)
        check(f"GET /hmi/{bad} refused", r.status_code == 404, r.status_code)
    listing = dexec("ls /workdir/core/generated/hmi/src")
    check(
        "the device holds only the manifest of the HMI, no screen code",
        listing.split() == ["hmi_access.c", "hmi_access.h"],
        listing,
    )

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


def open_live(viewer: Viewer, role: int, label: str) -> None:
    t = time.monotonic()
    viewer.open()
    status = viewer.wait_status("live", t)
    build = status.get("build") if status else None
    valid = isinstance(build, str) and re.fullmatch(r"[0-9a-f]{64}", build) is not None
    check(f"{label}: hmi_status live with its role and HMI build after open", status is not None and status.get("role") == role and valid, viewer.statuses)


def main() -> None:
    start_container()
    m = read_manifest()
    info(f"manifest: tags {m.tags}, pages {m.pages}")
    run, level = m.tags["RUN"], m.tags["LEVEL"]
    overview, status_page = m.pages["Overview"], m.pages["Status"]
    r = api("POST", "/create-user", body={"username": "admin", "password": "admin-pass"})
    check("bootstrap admin", r.status_code == 201, r.text)
    admin = login("admin", "admin-pass")
    for user, role in (("op1", "operator"), ("eve", "viewer"), ("op3", "operator")):
        r = api("POST", "/create-user", admin, {"username": user, "password": f"{user}-pass", "role": role})
        check(f"create {role} {user}", r.status_code == 201, r.text)
    operator = login("op1", "op1-pass")
    watcher = login("eve", "eve-pass")

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

    # TC13: the HMI plugin and the board plugin load together.
    check("TC13 board plugin init", wait_log(admin, "E2E-VPP init", 15))
    check("TC13 board plugin start_loop", wait_log(admin, "E2E-VPP start_loop", 15))
    check("TC13 HMI host serving next to it", wait_log(admin, "serving viewers on", 15))
    check("HMI host: screens match the PLC program", wait_log(admin, "screens match the PLC program", 15))
    for line in log_lines(runtime_log(admin) + container_log(), "E2E-VPP", "[HMI]", "HMI plugin", "screens match", "serving viewers"):
        info(f"log| {line}")

    client_files(admin)

    bad = socketio.Client(ssl_verify=False, reconnection=False)
    try:
        bad.connect(base(), namespaces=["/api/hmi"], auth={"token": "nope"}, transports=["websocket"])
        refused = False
    except socketio.exceptions.ConnectionError:
        refused = True
    finally:
        bad.disconnect()
    check("/api/hmi refuses a bad token", refused)

    # The operator: values of the screen shown, a write applied.
    v = Viewer(operator)
    v.connect()
    check("operator connects to /api/hmi", v.sio.connected)
    sid = v.sio.get_sid("/api/hmi")
    open_live(v, 2, "operator")
    check("operator viewer opened (host log)", wait_log(admin, "opened by op1 (operator)", 10))
    t = time.monotonic()
    v.show(overview)
    first = v.wait_values(lambda x: x.page == overview and run in x.records and level in x.records, t)
    check("SHOW Overview sends the snapshot of its tags", first is not None, v.values[-3:])
    check("Run is FALSE before the write", first is not None and first.records[run] == b"\x00", first)
    quiet = time.monotonic()
    time.sleep(2.0)
    idle = v.values_since(quiet)
    check("no values while nothing changes", len(idle) == 0, len(idle))

    t = time.monotonic()
    index = v.write(run, b"\x01")
    echoed = v.wait_values(lambda x: x.writes >= index and x.records.get(run) == b"\x01", t)
    check("the write's value comes back with the write count", echoed is not None, v.values[-3:])
    after = read_vars(admin)
    check("the write set Run = TRUE (debugger)", after is not None and after[0] == 1, after)
    check("write logged with the account", wait_log(admin, "write by op1: CONFIG0.RES0.INSTANCE0.MAIN.RUN = TRUE", 10))
    t = time.monotonic()
    rising = v.wait_values(lambda x: level in x.records, t)
    later = v.wait_values(lambda x: level in x.records and rising is not None and x.at > rising.at, t)
    check(
        "Level, changed by the PLC, keeps coming in the values",
        rising is not None and later is not None and struct.unpack("<f", later.records[level])[0] > struct.unpack("<f", rising.records[level])[0],
        (rising, later),
    )

    # Refused requests: none reaches the PLC.
    writes_before = write_count(admin)
    t = time.monotonic()
    v.write(level, struct.pack("<f", 1000.0))
    check("a write of Level, which Overview only reads, is refused", v.wait_refused("write", level, t), v.refused)
    t = time.monotonic()
    v.write(run, b"\x02")
    check("a BOOL of 2 is refused", v.wait_refused("write", run, t), v.refused)
    t = time.monotonic()
    v.send({"t": "w", "g": run, "v": "not bytes"})
    v.writes += 1
    check("a write whose value is not bytes is refused", v.wait_refused("write", NO_PAGE, t), v.refused)
    setpoint = m.tags["SETPOINT"]
    t = time.monotonic()
    v.write(setpoint, struct.pack("<h", 30000))
    check(
        "a Setpoint of 30000 is refused: Overview's slider and numeric input stop at 100",
        v.wait_refused("write", setpoint, t),
        v.refused,
    )
    check(
        "the out-of-range refusal is logged",
        wait_log(admin, "SETPOINT refused: the value is outside what the screen shown can set", 10),
    )
    t = time.monotonic()
    index = v.write(setpoint, struct.pack("<h", 100))
    check(
        "a Setpoint of 100 is applied and comes back",
        v.wait_values(lambda x: x.writes >= index and x.records.get(setpoint) == struct.pack("<h", 100), t)
        is not None,
    )
    writes_before += 1
    t = time.monotonic()
    v.show(77)
    check("a screen that does not exist is refused", v.wait_refused("show", 77, t), v.refused)
    t = time.monotonic()
    v.show(overview)
    check("showing Overview again serves it again", v.wait_values(lambda x: x.page == overview and run in x.records, t) is not None)
    check("no refused request was applied", write_count(admin) == writes_before, (writes_before, write_count(admin)))
    check("refusals are logged with the reason", wait_log(admin, "refused: the screen shown does not write it", 10))

    # A viewer-role account: only Status, which writes nothing.
    w = Viewer(watcher)
    w.connect()
    open_live(w, 3, "viewer role")
    t = time.monotonic()
    w.show(overview)
    check("a viewer may not show Overview", w.wait_refused("show", overview, t), w.refused)
    none = w.wait_values(lambda x: x.page == NO_PAGE, t)
    check("and is then served no tag", none is not None and not none.records, none)
    t = time.monotonic()
    w.write(run, b"\x00")
    check("a viewer's write of Run there is refused", w.wait_refused("write", run, t), w.refused)
    t = time.monotonic()
    w.show(status_page)
    status_values = w.wait_values(lambda x: x.page == status_page and run in x.records, t)
    check("a viewer shows Status and gets its values", status_values is not None, w.values[-2:])
    t = time.monotonic()
    w.write(run, b"\x00")
    check("a viewer's write of Run from Status (read only) is refused", w.wait_refused("write", run, t), w.refused)
    still = read_vars(admin)
    check("Run is still TRUE after the viewer's writes (debugger)", still is not None and still[0] == 1, still)
    check("the viewer's refusals are logged", wait_log(admin, "write by eve (viewer) of CONFIG0.RES0.INSTANCE0.MAIN.RUN refused", 10))
    w.sio.disconnect()

    # Negative control: the operator's same write is applied.
    t = time.monotonic()
    index = v.write(run, b"\x00")
    check("the operator's write of Run = FALSE is applied", v.wait_values(lambda x: x.writes >= index and x.records.get(run) == b"\x00", t) is not None)
    off = read_vars(admin)
    check("Run = FALSE (debugger)", off is not None and off[0] == 0, off)

    # The browser: taps, values, a refused write for the viewer role, sign-out.
    res = browser("operator-tap", user="op3", password="op3-pass", run=run, level=level)
    check("browser: the operator signs in, taps the switch and sees Level rise", "error" not in res, res)
    info(f"browser: {json.dumps(res)[:600]}")
    tapped = read_vars(admin)
    check("browser tap wrote Run = TRUE (debugger)", tapped is not None and tapped[0] == 1, tapped)
    check("browser write logged with its account", wait_log(admin, "write by op3: CONFIG0.RES0.INSTANCE0.MAIN.RUN = TRUE", 10))
    t = time.monotonic()
    index = v.write(run, b"\x00")
    v.wait_values(lambda x: x.writes >= index, t)
    res = browser("viewer-refused", user="eve", password="eve-pass", run=run, level=level, status=status_page, overview=overview)
    check("browser: a viewer sees Status, and its forged writes and shows are refused", "error" not in res, res)
    info(f"browser: {json.dumps(res)[:600]}")
    unchanged = read_vars(admin)
    check("Run unchanged by the viewer's browser (debugger)", unchanged is not None and unchanged[0] == 0, unchanged)
    res = browser("sign-out", user="op3", password="op3-pass")
    check("browser: the screen's Sign out ends the session", "error" not in res, res)
    res = browser("module-access", user="op3", password="op3-pass")
    check(
        "browser: hmi.wasm is not served before sign-in, is loaded once signed in, and is refused after sign-out",
        "error" not in res,
        res,
    )
    info(f"browser: {json.dumps(res)[:600]}")
    check("operator viewer unaffected by the other sessions", v.sio.connected and v.disconnects == 0)

    # TC11: stop and start keep the socket.
    t1 = time.monotonic()
    check("stop PLC", plc(admin, "stop", "STOPPED"))
    check("TC11 hmi_status stopped on PLC stop", v.wait_status("stopped", t1) is not None, v.statuses)
    time.sleep(2)
    check("TC11 socket stays connected while stopped", v.sio.connected and v.disconnects == 0)
    t2 = time.monotonic()
    check("start PLC", plc(admin, "start", "RUNNING"))
    check("TC11 hmi_status live again after start", v.wait_status("live", t2, 20) is not None, v.statuses)
    v.writes = 0
    v.show(overview)
    check("TC11 values resume once the screen is shown again", v.wait_values(lambda x: x.page == overview, t2) is not None)
    check("TC11 no reconnect (same sid, one connect)", v.sio.get_sid("/api/hmi") == sid and v.connects == 1 and v.disconnects == 0, (v.connects, v.disconnects))

    # TC27: a write and its value back, through the runtime.
    delays: list[float] = []
    current = latest(v, run) or b"\x00"
    for _ in range(100):
        want = b"\x00" if current == b"\x01" else b"\x01"
        t = time.monotonic()
        index = v.write(run, want)
        back = v.wait_values(lambda x, i=index, w_=want: x.writes >= i and x.records.get(run) == w_, t, 5)
        if back is not None:
            delays.append((back.at - t) * 1000)
            current = want
    delays.sort()
    if delays:
        info(
            f"TC27 write to value back over {len(delays)} writes: median {delays[len(delays) // 2]:.0f} ms, "
            f"p95 {delays[int(len(delays) * 0.95) - 1]:.0f} ms, max {delays[-1]:.0f} ms"
        )
    check("TC27 every write came back", len(delays) == 100, len(delays))
    check(f"TC27 each within {WRITE_ROUND_TRIP_MS} ms", bool(delays) and delays[-1] <= WRITE_ROUND_TRIP_MS, delays[-5:])

    # TC15: 1,000 writes, all applied, in order.
    writes_before = write_count(admin)
    final = b"\x00"
    for i in range(1000):
        final = b"\x01" if i % 2 == 0 else b"\x00"
        v.write(run, final)
        time.sleep(0.005)
    deadline = time.time() + 60
    while time.time() < deadline and write_count(admin) < writes_before + 1000:
        time.sleep(1)
    written = write_count(admin) - writes_before
    check("TC15 1,000 writes, 1,000 applied", written == 1000, written)
    last = read_vars(admin)
    check("TC15 the last write wins (Run FALSE)", last is not None and last[0] == 0, last)

    # TC12: an upload without hmi/ keeps the HMI, which is then out of date.
    zip2 = build_zip([(ARGS.bundle2, "")])
    t3 = time.monotonic()
    logs = upload(admin, zip2, "TC12 upload of a changed program without hmi/")
    check("TC12 build log: kept the installed HMI", any("kept the installed HMI" in l for l in logs))
    check("TC12 start PLC", plc(admin, "start", "RUNNING"))
    check("TC12 viewer gets out_of_date", v.wait_status("out_of_date", t3, 30) is not None, v.statuses)
    check("TC12 host log: screens out of date", wait_log(admin, "screens out of date", 10))
    writes_before = write_count(admin)
    t4 = time.monotonic()
    v.open()
    v.show(overview)
    v.write(run, b"\x01")
    time.sleep(3)
    check("TC12 no values while out of date", not v.values_since(t4), len(v.values_since(t4)))
    check("TC12 the write did nothing", write_count(admin) == writes_before, (writes_before, write_count(admin)))
    late = Viewer(operator)
    t5 = time.monotonic()
    late.connect()
    check("TC12 a viewer connecting later gets out_of_date", late.wait_status("out_of_date", t5, 10) is not None, late.statuses)
    late.sio.disconnect()
    check("TC12 the first viewer is still connected", v.sio.connected and v.disconnects == 0)
    v.sio.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="openplc-runtime:p9")
    parser.add_argument("--bundle", required=True, help="program bundle src/ of the first build")
    parser.add_argument("--hmi", required=True, help="directory holding hmi/")
    parser.add_argument("--bundle2", required=True, help="program bundle src/ of the changed program")
    parser.add_argument("--vpp", required=True, help="board plugin source directory (Makefile)")
    parser.add_argument("--playwright", required=True, help="path of the playwright package the browser half uses")
    parser.add_argument("--port", type=int, default=18543)
    parser.add_argument("--container", default="openplc-hmi-e2e", help="name of the test container (replaced)")
    parser.add_argument("--shots", default="hmi-e2e-shots", help="folder for the browser screenshots")
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
