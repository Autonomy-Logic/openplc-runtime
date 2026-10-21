# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Docker environment, REST client and independent MQTT consumer of the MQTT end-to-end tests.

Everything the product does is exercised through its real interfaces: the
runtime image built from this checkout, ``/api/upload-file`` with a program
compiled by the Editor pipeline, the real ``plc_main`` and real Mosquitto
brokers. The consumer is a separate paho client on the host.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import paho.mqtt.client as paho

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SPIKE = REPO / "tests" / "spikes" / "mqtt_transport"
SPIKE_IMAGE = "openplc-mqtt-spike:local"
DEFAULT_MOSQUITTO = "eclipse-mosquitto:2.0.22@sha256:199ea8ef2e35ec2b1b37e59cfd1dbae538ed4dfa4a2251a121a52215a6248a21"
PREFIX = "openplc-mqtt-e2e"


def docker(*args: str, check: bool = True, timeout: float = 600) -> str:
    done = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} failed: {done.stderr.strip()[:400]}")
    return done.stdout


def until(predicate: Callable[[], bool], seconds: float, step: float = 0.1) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def host_port(container: str, port: int) -> int:
    line = docker("port", container, f"{port}/tcp").strip().splitlines()[0]
    return int(line.rsplit(":", 1)[1])


@dataclass
class Pki:
    directory: Path

    def read(self, name: str) -> bytes:
        return (self.directory / name).read_bytes()

    @property
    def password(self) -> str:
        return self.read("password").decode()


class Network:
    """Network, PKI volume, brokers and a TCP proxy that can cut the runtime off the broker."""

    def __init__(self, mosquitto: str) -> None:
        self.mosquitto = mosquitto
        self.name = f"{PREFIX}-net"
        self.volume = f"{PREFIX}-pki"
        self.broker = f"{PREFIX}-broker"
        self.small = f"{PREFIX}-broker-small"
        self.proxy = f"{PREFIX}-proxy"
        self.pki = Pki(Path(tempfile.mkdtemp(prefix="mqtt-e2e-pki-")))
        self.broker_port = 0

    def up(self) -> None:
        self.down()
        docker("build", "-q", "-t", SPIKE_IMAGE, str(SPIKE))
        docker("network", "create", self.name)
        docker("volume", "create", self.volume)
        docker(
            "run", "--rm", "-v", f"{self.volume}:/pki", "-v", f"{SPIKE}:/spike:ro",
            SPIKE_IMAGE, "python", "/spike/pki.py", "/pki", "broker",
        )  # fmt: skip
        docker(
            "run", "--rm", "-v", f"{self.volume}:/pki:ro", "-v", f"{self.pki.directory}:/out",
            SPIKE_IMAGE, "sh", "-c", "cp /pki/* /out/",
        )  # fmt: skip
        self.start_broker()
        conf = self.pki.directory / "small.conf"
        conf.write_text(
            "log_dest stdout\npersistence false\nmax_packet_size 512\n"
            "listener 1883\nallow_anonymous true\n"
        )
        docker(
            "run", "-d", "--name", self.small, "--network", self.name,
            "--network-alias", "broker-small", "-v", f"{conf}:/mosquitto/config/mosquitto.conf:ro",
            self.mosquitto,
        )  # fmt: skip
        docker(
            "run", "-d", "--name", self.proxy, "--network", self.name,
            "--network-alias", "mqtt-proxy", "-v", f"{HERE}:/e2e:ro",
            SPIKE_IMAGE, "python", "/e2e/tcp_proxy.py", "1883", "broker", "1883",
        )  # fmt: skip

    def start_broker(self) -> None:
        docker("rm", "-f", self.broker, check=False)
        script = (
            'mosquitto_passwd -b -c /mosquitto/passwd spike-user "$(cat /pki/password)" '
            "&& chown mosquitto:mosquitto /mosquitto/passwd && chmod 0700 /mosquitto/passwd "
            "&& exec mosquitto -c /mosquitto/config/mosquitto.conf"
        )
        docker(
            "run", "-d", "--name", self.broker, "--hostname", "broker", "--network", self.name,
            "--network-alias", "broker", "-p", "127.0.0.1::1883", "-v", f"{self.volume}:/pki:ro",
            "-v", f"{SPIKE / 'mosquitto.conf'}:/mosquitto/config/mosquitto.conf:ro",
            self.mosquitto, "sh", "-c", script,
        )  # fmt: skip
        self.broker_port = host_port(self.broker, 1883)
        assert until(self._broker_ready, 15, 0.2), "broker did not start"

    def _broker_ready(self) -> bool:
        probe = paho.Client(
            paho.CallbackAPIVersion.VERSION2, client_id=f"probe-{uuid.uuid4().hex[:6]}"
        )
        try:
            probe.connect("127.0.0.1", self.broker_port, keepalive=5)
            probe.disconnect()
            return True
        except OSError:
            return False

    def _broker_logs(self) -> str:
        done = subprocess.run(
            ["docker", "logs", self.broker], capture_output=True, text=True, check=False
        )
        return done.stdout + done.stderr

    def broker_version(self) -> str:
        return docker("exec", self.broker, "mosquitto", "-h", check=False).splitlines()[0]

    def cut(self) -> None:
        docker("stop", "-t", "0", self.proxy)

    def restore(self) -> None:
        docker("start", self.proxy)

    def down(self) -> None:
        for name in (self.proxy, self.small, self.broker):
            docker("rm", "-f", name, check=False)
        docker("volume", "rm", "-f", self.volume, check=False)
        docker("network", "rm", self.name, check=False)

    def cleanup(self) -> None:
        self.down()
        shutil.rmtree(self.pki.directory, ignore_errors=True)


class Runtime:
    """The runtime container and its REST API, verified against the runtime's own certificate."""

    def __init__(self, image: str, network: Network, extra_args: tuple[str, ...] = ()) -> None:
        self.image = image
        self.network = network
        self.extra_args = extra_args
        self.name = f"{PREFIX}-runtime"
        self.port = 0
        self.token = ""
        self._context: ssl.SSLContext | None = None
        self._user = {"username": "e2e", "password": "e2e-pass"}
        self.cert_dir = Path(tempfile.mkdtemp(prefix="mqtt-e2e-rt-"))

    def up(self) -> None:
        docker("rm", "-f", self.name, check=False)
        docker(
            "run", "-d", "--name", self.name, "--network", self.network.name,
            "--network-alias", "runtime", "--cap-add=sys_nice", "--ulimit", "rtprio=99",
            "--ulimit", "memlock=-1", "-p", "127.0.0.1::8443", "-p", "127.0.0.1::5020",
            *self.extra_args, self.image,
        )  # fmt: skip
        self.port = host_port(self.name, 8443)
        cert = self.cert_dir / "runtime.pem"
        source = f"{self.name}:/workdir/webserver/certOPENPLC.pem"
        assert until(
            lambda: docker("cp", source, str(cert), check=False) == "" and cert.exists(), 90, 0.5
        ), "runtime certificate was not generated"
        self._context = ssl.create_default_context(cafile=str(cert))
        assert until(lambda: self._reachable(), 90, 0.5), "runtime API did not come up"
        assert self.request("POST", "/api/create-user", dict(self._user))[0] == 201
        self._login()

    def _login(self) -> None:
        self.token = ""
        status, body = self.request("POST", "/api/login", dict(self._user))
        assert status == 200 and isinstance(body, dict), body
        self.token = str(body["access_token"])

    def _reachable(self) -> bool:
        try:
            return self.request("GET", "/api/version")[0] == 200
        except (OSError, urllib.error.URLError):
            return False

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        multipart: list[tuple[str, str, bytes, str]] | None = None,
        timeout: float = 60,
    ) -> tuple[int, object]:
        status, reply = self._send(method, path, body, multipart, timeout)
        expired = isinstance(reply, dict) and "expired" in str(reply.get("msg", ""))
        if status == 401 and expired and self.token:
            self._login()
            status, reply = self._send(method, path, body, multipart, timeout)
        return status, reply

    def _send(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None,
        multipart: list[tuple[str, str, bytes, str]] | None,
        timeout: float,
    ) -> tuple[int, object]:
        headers = {}
        data = None
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if multipart is not None:
            boundary = uuid.uuid4().hex
            chunks = []
            for name, filename, content, ctype in multipart:
                chunks.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
                    + content
                    + b"\r\n"
                )
            data = b"".join(chunks) + f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        req = urllib.request.Request(
            f"https://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, context=self._context, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        text = raw.decode("utf-8", errors="replace")
        try:
            return status, json.loads(text) if text else None
        except ValueError:
            return status, text

    def get(self, path: str) -> dict[str, object]:
        status, body = self.request("GET", path)
        assert status == 200 and isinstance(body, dict), (path, status, body)
        return body

    def mqtt_status(self) -> dict[str, object]:
        return self.get("/api/mqtt/status")

    def scan_stats(self) -> dict[str, object]:
        body = self.get("/api/status?include_stats=true")
        tasks = body.get("timing_stats", {}).get("tasks", [])  # type: ignore[union-attr]
        return tasks[0] if tasks else {}

    def plc_state(self) -> str:
        return str(self.get("/api/status")["status"]).strip()

    def start_plc(self) -> None:
        self.get("/api/start-plc")
        assert until(lambda: self.plc_state() == "STATUS:RUNNING", 30, 0.2), self.plc_state()

    def stop_plc(self) -> float:
        start = time.monotonic()
        self.get("/api/stop-plc")
        assert until(lambda: self.plc_state() == "STATUS:STOPPED", 30, 0.05), self.plc_state()
        return time.monotonic() - start

    def stage(self, request: dict[str, object], secrets: dict[str, bytes]) -> dict[str, object]:
        parts = [("request", "request.json", json.dumps(request).encode(), "application/json")]
        parts += [(k, k, v, "application/octet-stream") for k, v in secrets.items()]
        status, body = self.request("POST", "/api/mqtt/credentials/staging", multipart=parts)
        assert status == 201, body
        assert isinstance(body, dict)
        return body

    def deploy(
        self, payload: Path, mutate: Callable[[dict[str, object]], None] | None = None
    ) -> dict[str, object]:
        """Upload the bundle (optionally rewriting conf/mqtt.json), wait for the build, return the result."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(payload.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(payload).as_posix()
                content = path.read_bytes()
                if rel == "conf/mqtt.json" and mutate is not None:
                    doc = json.loads(content)
                    mutate(doc)
                    content = json.dumps(doc).encode()
                archive.writestr(rel, content)
        status, body = self.request(
            "POST", "/api/upload-file",
            multipart=[("file", "program.zip", buf.getvalue(), "application/zip")], timeout=120,
        )  # fmt: skip
        assert status == 200 and isinstance(body, dict), body
        if body.get("UploadFileFail"):
            return body
        assert until(
            lambda: self.get("/api/compilation-status")["status"] in ("SUCCESS", "FAILED"), 300, 0.5
        )
        result = self.get("/api/compilation-status")
        return {"build": result["status"], "logs": result["logs"]}

    def exec(self, *command: str) -> str:
        return docker("exec", self.name, *command, check=False)

    def logs(self) -> str:
        done = subprocess.run(
            ["docker", "logs", self.name], capture_output=True, text=True, check=False
        )
        return done.stdout + done.stderr

    def cleanup(self) -> None:
        docker("rm", "-f", self.name, check=False)
        shutil.rmtree(self.cert_dir, ignore_errors=True)


@dataclass
class Message:
    topic: str
    payload: bytes
    qos: int
    retain: bool
    at: float


@dataclass
class Consumer:
    """Independent subscriber on the host; it shares no code with the product serializers."""

    port: int
    topics: list[str]
    client_id: str = field(default_factory=lambda: f"e2e-consumer-{uuid.uuid4().hex[:8]}")
    messages: list[Message] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._subscribed = threading.Event()
        self.client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_subscribe = lambda *_a: self._subscribed.set()

    def _on_connect(self, client: paho.Client, *_args: object) -> None:
        for topic in self.topics:
            client.subscribe(topic, qos=1)

    def _on_message(self, _client: paho.Client, _userdata: object, msg: paho.MQTTMessage) -> None:
        with self._lock:
            self.messages.append(
                Message(msg.topic, bytes(msg.payload), msg.qos, bool(msg.retain), time.monotonic())
            )

    def start(self) -> Consumer:
        self.client.connect("127.0.0.1", self.port, keepalive=30)
        self.client.loop_start()
        assert self._subscribed.wait(10), "consumer did not subscribe"
        return self

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def snapshot(self) -> list[Message]:
        with self._lock:
            return list(self.messages)

    def clear(self) -> None:
        with self._lock:
            self.messages.clear()

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        self.client.publish(topic, payload, qos=0, retain=retain).wait_for_publish(5)


def modbus_read_holding(port: int, address: int) -> int:
    """One Modbus/TCP read of a holding register, hand-built (no Modbus library on the host)."""
    request = struct.pack(">HHHBBHH", 1, 0, 6, 1, 3, address, 1)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        reply = sock.recv(64)
    _tid, _proto, _length, _unit, function, count, value = struct.unpack(">HHHBBBh", reply[:11])
    assert function == 3 and count == 2, reply.hex()
    return value


def env_paths() -> tuple[str, Path] | None:
    image = os.environ.get("MQTT_E2E_RUNTIME_IMAGE", "")
    payloads = os.environ.get("MQTT_E2E_PAYLOADS", "")
    if not image or not payloads:
        return None
    return image, Path(payloads)
