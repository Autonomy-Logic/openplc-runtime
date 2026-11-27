# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Probes a running runtime container from a container that shares its network.

Run with a Python 3.13 interpreter of the image, the script on stdin: python -I - <probe> [args].
Prints one line and exits 0 when the expectation of the probe holds, non-zero otherwise.
"""

import base64
import hashlib
import hmac
import http.client
import io
import json
import os
import random
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import zipfile

HOST = "127.0.0.1"
PORT = 8443
USERNAME = "image-test"
PASSWORD = "image-test-password"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_VERIFIABLE = 4
TLS_VERSIONS = {
    "1.0": ssl.TLSVersion.TLSv1,
    "1.1": ssl.TLSVersion.TLSv1_1,
    "1.2": ssl.TLSVersion.TLSv1_2,
    "1.3": ssl.TLSVersion.TLSv1_3,
}
REFUSED_AUTH_STATUSES = (401, 422)
MIB = 1024 * 1024


class ProbeError(Exception):
    """A probe could not reach the state its verdict needs."""


def done(code: int, line: str) -> None:
    """Prints the verdict line and exits with the code."""
    print(line)
    sys.exit(code)


def client_context(version: ssl.TLSVersion) -> ssl.SSLContext:
    """A client context that offers only the given TLS version and every cipher."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = version
        ctx.maximum_version = version
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    return ctx


def handshake(ctx: ssl.SSLContext, port: int) -> str:
    """Completes a TLS handshake with HOST:port and returns the negotiated version."""
    raw = socket.create_connection((HOST, port), timeout=10)
    with raw, ctx.wrap_socket(raw, server_hostname="localhost") as tls:
        return tls.version() or ""


def weak_server(directory: str) -> tuple[socket.socket, ssl.SSLContext]:
    """A listening socket and a server context that accept every TLS version from 1.0."""
    key = os.path.join(directory, "key.pem")
    cert = os.path.join(directory, "cert.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"]
        + ["-subj", "/CN=localhost", "-keyout", key, "-out", cert],
        check=True,
        capture_output=True,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    ctx.load_cert_chain(cert, key)
    listener = socket.create_server((HOST, 0))
    return listener, ctx


def client_reaches(version: ssl.TLSVersion) -> str:
    """Empty when this client completes a handshake at the version with a local server, else why."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            listener, server_ctx = weak_server(directory)
        except (OSError, subprocess.CalledProcessError, ssl.SSLError) as exc:
            return f"no reference server: {exc}"

        def serve() -> None:
            try:
                conn, _ = listener.accept()
                with server_ctx.wrap_socket(conn, server_side=True):
                    pass
            except (OSError, ssl.SSLError):
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            got = handshake(client_context(version), listener.getsockname()[1])
        except (OSError, ssl.SSLError) as exc:
            return f"{exc}"
        finally:
            thread.join(timeout=10)
            listener.close()
        return "" if got == version_name(version) else f"negotiated {got}"


def version_name(version: ssl.TLSVersion) -> str:
    """The name ssl.SSLSocket.version() gives a TLS version."""
    return {"TLSv1": "TLSv1", "TLSv1_1": "TLSv1.1", "TLSv1_2": "TLSv1.2", "TLSv1_3": "TLSv1.3"}[
        version.name
    ]


def probe_tls(name: str, expect: str) -> None:
    """Checks that port 8443 accepts or refuses a handshake limited to one TLS version."""
    version = TLS_VERSIONS[name]
    unreachable = client_reaches(version)
    if unreachable:
        done(
            EXIT_NOT_VERIFIABLE,
            f"not verifiable with this client: TLS {name} to a local server failed: {unreachable}",
        )
    try:
        got = handshake(client_context(version), PORT)
    except (OSError, ssl.SSLError) as exc:
        if expect == "refused":
            done(EXIT_OK, f"TLS {name} refused: {exc}")
        done(EXIT_FAILED, f"TLS {name} refused: {exc}")
    if expect == "accepted" and got == version_name(version):
        done(EXIT_OK, f"TLS {name} accepted ({got})")
    done(EXIT_FAILED, f"TLS {name} accepted ({got})")


def request(
    method: str,
    path: str,
    token: str = "",
    body: bytes = b"",
    content_type: str = "",
) -> tuple[int, str]:
    """Sends one HTTPS request to the webserver and returns the status and the body."""
    ctx = client_context(ssl.TLSVersion.TLSv1_2)
    ctx.maximum_version = ssl.TLSVersion.MAXIMUM_SUPPORTED
    conn = http.client.HTTPSConnection(HOST, PORT, context=ctx, timeout=120)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content_type:
        headers["Content-Type"] = content_type
    try:
        conn.request(method, path, body=body or None, headers=headers)
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        conn.close()


def post_json(path: str, payload: dict[str, str]) -> tuple[int, str]:
    """Posts a JSON body."""
    return request("POST", path, body=json.dumps(payload).encode(), content_type="application/json")


def login() -> str:
    """Creates the test account when the database has none and returns an access token for it."""
    post_json("/api/create-user", {"username": USERNAME, "password": PASSWORD})
    status, body = post_json("/api/login", {"username": USERNAME, "password": PASSWORD})
    try:
        token = json.loads(body).get("access_token", "")
    except (ValueError, AttributeError):
        token = ""
    if status != 200 or not token:
        raise ProbeError(f"login failed: {status} {body[:200]}")
    return token


def upload(token: str, filename: str, data: bytes) -> tuple[int, str]:
    """Posts a file to /api/upload-file as multipart form data."""
    boundary = "image-test-boundary-7d1f"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    )
    body = head.encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return request(
        "POST",
        "/api/upload-file",
        token=token,
        body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def resign(token: str, key: bytes, claims: dict[str, int]) -> str:
    """The token with its claims updated, signed with HS256 under the key."""
    header, payload, _ = token.split(".")
    data = json.loads(b64url_decode(payload))
    data.update(claims)
    signing_input = f"{header}.{b64url(json.dumps(data, separators=(',', ':')).encode())}"
    signature = hmac.new(key, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{b64url(signature)}"


def probe_auth(kind: str) -> None:
    """Checks an upload with no token, a token of another key or an expired token is refused."""
    token = login()
    probe_file = b"not an archive"
    status, body = upload(token, "probe.zip", probe_file)
    if status != 200 or "UploadFileFail" not in body:
        done(EXIT_FAILED, f"the valid token is refused too ({status} {body[:120]}); cannot tell")
    if kind == "none":
        bad = ""
    elif kind == "other-key":
        bad = resign(token, b"image-test-other-key-" + b"0" * 43, {})
    elif kind == "expired":
        key = os.environ.get("IMAGE_TEST_JWT_KEY", "")
        if not key:
            done(EXIT_FAILED, "the webserver's JWT key was not passed; cannot sign an old token")
        now = int(time.time())
        bad = resign(token, key.encode(), {"iat": now - 7200, "nbf": now - 7200, "exp": now - 3600})
        if resign(token, key.encode(), {}) != token:
            done(EXIT_FAILED, "the key passed does not sign the webserver's tokens; cannot tell")
    else:
        done(EXIT_FAILED, f"unknown token kind {kind}")
    status, body = upload(bad, "probe.zip", probe_file)
    line = f"{status} {' '.join(body.split())[:160]}"
    if status in REFUSED_AUTH_STATUSES:
        done(EXIT_OK, f"refused: {line}")
    done(EXIT_FAILED, f"accepted: {line}")


def archive(kind: str) -> bytes:
    """A program archive with one entry that must be refused."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if kind == "dotdot":
            zf.writestr("program.st", "PROGRAM main\nEND_PROGRAM\n")
            zf.writestr(zipfile.ZipInfo("../../image-test-marker-dotdot.st"), "escaped\n")
        elif kind == "absolute":
            zf.writestr("program.st", "PROGRAM main\nEND_PROGRAM\n")
            zf.writestr("notes.txt", "second top-level entry\n")
            zf.writestr(zipfile.ZipInfo("/tmp/image-test-marker-absolute.st"), "escaped\n")
        elif kind == "expanding":
            rng = random.Random(440)
            for n in range(7):
                block = bytearray(8 * MIB)
                for at in range(0, len(block), 512):
                    block[at] = rng.randrange(256)
                zf.writestr(f"image-test-marker-expanding-{n}.st", bytes(block))
        else:
            raise ProbeError(f"unknown archive kind {kind}")
    return buf.getvalue()


def archive_shape(data: bytes) -> str:
    """For the expanding archive: each entry under 10 MiB and 1000:1, the total over 50 MiB."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
    total = sum(i.file_size for i in infos)
    worst = max(i.file_size / max(i.compress_size, 1) for i in infos)
    if total <= 50 * MIB or max(i.file_size for i in infos) >= 10 * MIB or worst >= 1000:
        return f"the archive does not have its intended shape: total {total}, ratio {worst:.0f}"
    return ""


def probe_archive(kind: str) -> None:
    """Checks that an archive with a dangerous entry is refused by an authenticated upload."""
    token = login()
    status, body = request("GET", "/api/compilation-status", token=token)
    if status != 200 or '"COMPILING"' in body:
        done(EXIT_FAILED, f"the runtime is not idle ({status} {body[:120]}); the check cannot tell")
    data = archive(kind)
    if kind == "expanding":
        wrong = archive_shape(data)
        if wrong:
            done(EXIT_FAILED, wrong)
    status, body = upload(token, f"{kind}.zip", data)
    line = f"{status} {' '.join(body.split())[:200]}"
    try:
        answer = json.loads(body)
    except ValueError:
        done(EXIT_FAILED, f"not a JSON answer: {line}")
    refused = answer.get("UploadFileFail") and answer.get("CompilationStatus") == "FAILED"
    if status == 200 and refused:
        done(EXIT_OK, f"refused: {answer['UploadFileFail']}")
    done(EXIT_FAILED, f"accepted: {line}")


def probe_plugin(dependency: str, entry: str) -> None:
    """Checks the venv runs Python 3.13, imports the dependency and loads the entry module."""
    import importlib

    if sys.version_info[:2] != (3, 13):
        done(EXIT_FAILED, f"{sys.executable} runs Python {sys.version.split()[0]}")
    site = os.path.join(sys.prefix, "lib", "python3.13", "site-packages")
    if not os.path.isdir(site):
        done(EXIT_FAILED, f"{site} does not exist")
    sys.path.insert(0, os.path.dirname(entry))
    try:
        importlib.import_module(dependency)
        module = importlib.import_module(os.path.splitext(os.path.basename(entry))[0])
    except (ImportError, SyntaxError, OSError, RuntimeError, AttributeError, ValueError) as exc:
        done(EXIT_FAILED, f"{type(exc).__name__}: {exc}")
    hooks = ("init", "start_loop", "stop_loop", "cleanup")
    missing = [n for n in hooks if not callable(getattr(module, n, None))]
    if missing:
        done(EXIT_FAILED, f"{entry} has no {', '.join(missing)}")
    name = os.path.basename(entry)
    done(EXIT_OK, f"Python {sys.version.split()[0]}, {dependency} and {name} load")


def opcua_config(policy: str, mode: str, port: int) -> list[dict[str, object]]:
    """An OPC-UA plugin configuration with an insecure profile disabled and one profile enabled."""
    return [
        {
            "name": "image_test_opcua",
            "protocol": "OPC-UA",
            "config": {
                "format_version": 2,
                "server": {
                    "name": "Image test server",
                    "application_uri": "urn:openplc:image-test",
                    "product_uri": "urn:openplc:image-test",
                    "endpoint_url": f"opc.tcp://{HOST}:{port}",
                    "security_profiles": [
                        {
                            "name": "insecure",
                            "enabled": False,
                            "security_policy": "None",
                            "security_mode": "None",
                            "auth_methods": ["Anonymous"],
                        },
                        {
                            "name": "configured",
                            "enabled": True,
                            "security_policy": policy,
                            "security_mode": mode,
                            "auth_methods": ["Anonymous"],
                        },
                    ],
                },
                "security": {
                    "server_certificate_strategy": "auto_self_signed",
                    "server_certificate_custom": None,
                    "server_private_key_custom": None,
                    "trusted_client_certificates": [],
                },
                "users": [],
                "address_space": {
                    "namespace_uri": "urn:openplc:image-test",
                    "variables": [],
                    "structures": [],
                    "arrays": [],
                },
                "cycle_time_ms": 100,
            },
        }
    ]


def policy_name(uri: str) -> str:
    """The policy name at the end of an OPC-UA security policy URI."""
    return uri.rsplit("#", 1)[-1]


class IdleBufferAccess:
    """Stands in for the PLC memory accessor of a server with an empty address space."""

    is_valid = True
    error_msg = ""
    runtime_args = None


def probe_opcua(policy: str, mode: str) -> None:
    """Starts the plugin's server with one profile enabled and checks the endpoints it offers."""
    import asyncio

    plugin_dir = "/workdir/core/src/drivers/plugins/python/opcua"
    sys.path[:0] = [plugin_dir, os.path.dirname(plugin_dir)]
    from asyncua import Client
    from config import load_config
    from server import OpcuaServerManager

    port = 4840
    work = tempfile.mkdtemp()
    path = os.path.join(work, "opcua.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(opcua_config(policy, mode, port), f)
    config = load_config(path)
    if config is None:
        done(EXIT_FAILED, "the plugin did not load the configuration")
    manager = OpcuaServerManager(config, IdleBufferAccess(), work)
    thread = threading.Thread(target=lambda: asyncio.run(manager.run()), daemon=True)
    thread.start()

    async def endpoints() -> set[str]:
        deadline = time.monotonic() + 90
        while True:
            try:
                client = Client(f"opc.tcp://{HOST}:{port}", timeout=5)
                found = await client.connect_and_get_server_endpoints()
                return {f"{policy_name(e.SecurityPolicyUri)}/{e.SecurityMode.name}" for e in found}
            except (OSError, asyncio.TimeoutError):
                if time.monotonic() > deadline or not thread.is_alive():
                    raise ProbeError("the OPC-UA server did not answer on port 4840")
                await asyncio.sleep(0.5)

    try:
        offered = asyncio.run(endpoints())
    finally:
        manager.request_stop()
        thread.join(timeout=10)
    line = f"endpoints offer {', '.join(sorted(offered)) or 'nothing'}"
    if offered == {f"{policy}/{mode}"}:
        done(EXIT_OK, line)
    done(EXIT_FAILED, f"{line}; expected only {policy}/{mode}")


def main(argv: list[str]) -> None:
    """Dispatches one probe."""
    probes = {
        "tls": lambda a: probe_tls(a[0], a[1]),
        "auth": lambda a: probe_auth(a[0]),
        "archive": lambda a: probe_archive(a[0]),
        "plugin": lambda a: probe_plugin(a[0], a[1]),
        "opcua": lambda a: probe_opcua(a[0], a[1]),
    }
    if not argv or argv[0] not in probes:
        done(EXIT_FAILED, f"usage: security_probe.py {{{','.join(probes)}}} ...")
    try:
        probes[argv[0]](argv[1:])
    except (ProbeError, ImportError, OSError, http.client.HTTPException) as exc:
        done(EXIT_FAILED, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main(sys.argv[1:])
