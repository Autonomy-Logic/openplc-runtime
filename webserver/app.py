import sys

# Parse --print-debug argument before any logger imports
# This must happen first so LoggerConfig.print_debug is set before loggers are created
_print_debug = "--print-debug" in sys.argv

from webserver.logger.config import LoggerConfig

LoggerConfig.print_debug = _print_debug

import errno
import json
import os
import platform
import shutil
import ssl
import threading
from pathlib import Path
from typing import Callable, Final, Optional

import flask
import flask_login

from webserver import project_snapshot
from webserver.credentials import CertGen
from webserver.debug_websocket import init_debug_websocket
from webserver.discovery.discovery_routes import discovery_bp
from webserver.discovery.network_discovery import (
    responder as network_discovery_responder,
)
from webserver.logger import get_logger
from webserver.plcapp_management import (
    MAX_FILE_SIZE,
    BuildStatus,
    analyze_zip,
    apply_retain_conf,
    apply_vpp_plugin_conf,
    build_state,
    run_compile,
    safe_extract,
    update_plugin_configurations,
)
from webserver.restapi import (
    app_restapi,
    apply_user_schema_migrations,
    db,
    register_callback_get,
    register_callback_post,
    repair_missing_admin,
    restapi_bp,
)
from webserver.runtime_info import runtime_info_bp
from webserver.runtimemanager import RuntimeManager

logger, _ = get_logger("logger", use_buffer=True)

app = flask.Flask(__name__)
app.secret_key = str(os.urandom(16))

# A backstop at the HTTP layer, under everything the routes do.
#
# Individual handlers check their own parts, but those checks run after Werkzeug
# has already parsed (and spooled to disk) the request. This bounds the whole
# body first, so an oversized upload is refused as 413 before any of it is
# stored. Sized to hold the largest legitimate request -- a program zip and a
# project snapshot together -- plus room for the multipart framing.
app.config["MAX_CONTENT_LENGTH"] = (
    MAX_FILE_SIZE + project_snapshot.MAX_SNAPSHOT_BYTES + (8 * 1024 * 1024)
)
login_manager = flask_login.LoginManager()
login_manager.init_app(app)

runtime_manager = RuntimeManager(
    runtime_path="./build/plc_main",
    plc_socket="/run/runtime/plc_runtime.socket",
    log_socket="/run/runtime/log_runtime.socket",
    print_debug=_print_debug,
)

runtime_manager.start()

# UDP discovery responder so the editor can find this runtime on the LAN.
# Failure to bind is logged and ignored — discovery is a convenience, not a
# hard dependency.
network_discovery_responder.start()

# Store in Flask app config so blueprints can access via current_app
# without triggering a re-import of this module (which would create
# a duplicate RuntimeManager when run with python -m webserver.app).
app_restapi.config["RUNTIME_MANAGER"] = runtime_manager

BASE_DIR: Final[Path] = Path(__file__).parent
CERT_FILE: Final[Path] = (BASE_DIR / "certOPENPLC.pem").resolve()
KEY_FILE: Final[Path] = (BASE_DIR / "keyOPENPLC.pem").resolve()
HOSTNAME: Final[str] = "localhost"


def handle_start_plc(data: dict) -> dict:
    response = runtime_manager.start_plc()
    return {"status": response}


def handle_stop_plc(data: dict) -> dict:
    response = runtime_manager.stop_plc()
    return {"status": response}


def handle_runtime_logs(data: dict) -> dict:
    if "id" in data:
        min_id = int(data["id"])
    else:
        min_id = None
    if "level" in data:
        level = data["level"]
    else:
        level = None
    response = runtime_manager.get_logs(min_id=min_id, level=level)
    return {"runtime-logs": response}


def handle_compilation_status(data: dict) -> dict:
    return {
        "status": build_state.status.name,
        "logs": build_state.logs[:],  # all lines
        "exit_code": build_state.exit_code,
    }


def parse_timing_stats(stats_response: Optional[str]) -> Optional[dict]:
    """
    Parse the STATS response from the runtime.
    Expected format: STATS:{json_object}
    Returns the parsed JSON object or None if parsing fails.
    """
    if stats_response is None:
        return None

    # Remove the STATS: prefix
    if stats_response.startswith("STATS:"):
        json_str = stats_response[6:].strip()
    else:
        return None

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


def parse_switch_position(switch_response: Optional[str]) -> Optional[str]:
    """
    Parse the SWITCH response from the runtime.
    Expected format: ``SWITCH:RUN`` / ``SWITCH:STOP``.
    Returns ``"run"`` / ``"stop"``, or None when the response is unusable.
    """
    if switch_response is None:
        return None
    value = switch_response.strip()
    if value == "SWITCH:RUN":
        return "run"
    if value == "SWITCH:STOP":
        return "stop"
    return None


def handle_status(data: dict) -> dict:
    response = runtime_manager.status_plc()
    if response is None:
        return {"status": "No response from runtime"}

    result: dict = {"status": response}

    # Mode-switch position, so the editor can block a start before sending it
    # rather than relying on the runtime's refusal alone. Additive: the existing
    # `status` key is untouched, and an older editor simply ignores this field.
    # A runtime with no switch-aware plugin always reports "run".
    switch_position = parse_switch_position(runtime_manager.switch_plc())
    if switch_position is not None:
        result["switchPosition"] = switch_position

    # Only fetch timing stats if explicitly requested via include_stats parameter.
    # This avoids acquiring the stats mutex on every status poll, which could
    # introduce latency to the critical PLC scan cycle.
    include_stats = data.get("include_stats", "").lower() == "true"
    if include_stats:
        stats_response = runtime_manager.stats_plc()
        timing_stats = parse_timing_stats(stats_response)
        if timing_stats is not None:
            result["timing_stats"] = timing_stats

    return result


def handle_ping(data: dict) -> dict:
    response = runtime_manager.ping()
    return {"status": response}


def handle_list_serial_ports(data: dict) -> dict:
    """
    List available serial ports on the system.

    Returns:
        {
            "ports": [
                {"device": "/dev/ttyUSB0", "description": "USB-Serial Controller"},
                {"device": "/dev/ttyACM0", "description": "Arduino Uno"},
                ...
            ]
        }
    """
    try:
        import serial.tools.list_ports

        ports = serial.tools.list_ports.comports()
        port_list = [
            {
                "device": port.device,
                "description": port.description or port.device,
            }
            for port in ports
        ]
        return {"ports": port_list}
    except ImportError:
        return {"error": "pyserial not installed", "ports": []}
    except Exception as e:
        return {"error": str(e), "ports": []}


def handle_switch(data: dict) -> dict:
    """
    Report the run/stop mode-switch position on its own, for callers that want
    it without a full status poll. Devices with no switch-aware VPP plugin
    always answer "run".
    """
    position = parse_switch_position(runtime_manager.switch_plc())
    return {"switchPosition": position if position is not None else "unknown"}


GET_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "start-plc": handle_start_plc,
    "stop-plc": handle_stop_plc,
    "runtime-logs": handle_runtime_logs,
    "compilation-status": handle_compilation_status,
    "status": handle_status,
    "ping": handle_ping,
    "serial-ports": handle_list_serial_ports,
    "switch": handle_switch,
}


def restapi_callback_get(argument: str, data: dict) -> dict:
    """
    Dispatch GET callbacks by argument.
    """
    # logger.debug("GET | Received argument: %s, data: %s", argument, data)
    handler = GET_HANDLERS.get(argument)
    if handler:
        return handler(data)
    return {"error": "Unknown argument"}


def stage_project_snapshot() -> str:
    """Stage the optional source-project snapshot that rides along with an upload.

    Returns an empty string when there was nothing to store or it was stored,
    and a human-readable reason otherwise. Never raises: the snapshot is the
    optional half of the request and must not be able to fail a program upload.

    The two fields are deliberately separate from ``program.zip``. Inside it
    they would run through ``analyze_zip`` and be extracted into
    ``core/generated``, where the next upload would wipe them and the compiler
    would try to build them. The metadata is separate from the archive for the
    same reason in reverse: the runtime never opens the archive, so anything it
    needs to say about the stored project has to arrive already parsed.
    """
    snapshot_file = flask.request.files.get("snapshot")
    if snapshot_file is None:
        return ""

    raw_metadata = flask.request.form.get("snapshot_metadata")
    if not raw_metadata:
        return "Snapshot ignored: the upload carried an archive but no snapshot_metadata"

    try:
        metadata = project_snapshot.normalize_metadata(json.loads(raw_metadata))
    except json.JSONDecodeError as e:
        return f"Snapshot ignored: snapshot_metadata is not valid JSON ({e})"
    except project_snapshot.SnapshotError as e:
        return f"Snapshot ignored: {e}"

    # Bounded read, before the bytes exist rather than after.
    #
    # `stage()` also enforces the cap, but only once the whole part is already
    # in memory -- and this route is authenticated without an admin gate, so any
    # account on the device could post an arbitrarily large `snapshot` field and
    # have it spooled to disk and then pulled into RAM before anything refused
    # it. On the hardware this runtime targets that is a disk-fill followed by
    # an OOM.
    #
    # `content_length` on a multipart part is client-supplied and often absent,
    # so it is a fast path and not the guard. Reading one byte past the cap and
    # stopping is what actually bounds this, whatever the client claimed.
    declared = snapshot_file.content_length
    if declared and declared > project_snapshot.MAX_SNAPSHOT_BYTES:
        return (
            f"Snapshot ignored: archive is too large "
            f"({declared} bytes, limit {project_snapshot.MAX_SNAPSHOT_BYTES})"
        )

    try:
        blob = snapshot_file.read(project_snapshot.MAX_SNAPSHOT_BYTES + 1)
    except (OSError, IOError) as e:
        return f"Snapshot ignored: could not read the uploaded archive ({e})"

    if len(blob) > project_snapshot.MAX_SNAPSHOT_BYTES:
        return (
            f"Snapshot ignored: archive is too large "
            f"(limit {project_snapshot.MAX_SNAPSHOT_BYTES} bytes)"
        )

    try:
        project_snapshot.stage(blob, metadata)
    except project_snapshot.SnapshotError as e:
        return f"Snapshot ignored: {e}"

    return ""


def handle_upload_file(data: dict) -> dict:
    if build_state.status == BuildStatus.COMPILING:
        return {
            "UploadFileFail": "Runtime is compiling another program, please wait",
            "CompilationStatus": build_state.status.name,
        }

    build_state.clear()  # remove all previous build logs

    if "file" not in flask.request.files:
        build_state.status = BuildStatus.FAILED
        return {
            "UploadFileFail": "No file part in the request",
            "CompilationStatus": build_state.status.name,
        }

    zip_file = flask.request.files["file"]

    if zip_file.content_length > MAX_FILE_SIZE:
        build_state.status = BuildStatus.FAILED
        return {
            "UploadFileFail": "File is too large",
            "CompilationStatus": build_state.status.name,
        }

    try:
        build_state.status = BuildStatus.UNZIPPING
        safe, valid_files = analyze_zip(zip_file)
        if not safe:
            build_state.status = BuildStatus.FAILED
            return {
                "UploadFileFail": "Uploaded ZIP file failed safety checks",
                "CompilationStatus": build_state.status.name,
            }

        extract_dir = "core/generated"

        # Point of no return: past here the program on the device is being
        # replaced, so the stored project snapshot must go with it. Clearing
        # here rather than on arrival means a rejected upload (bad zip, too
        # large, runtime busy) leaves the previous program AND its snapshot
        # untouched, which is the pair that is actually still true.
        #
        # An upload carrying no snapshot therefore erases the stored one --
        # that is the point. Older editors, openplc-cli and any third-party
        # client keep working, and the device stops advertising a project it
        # is no longer running.
        project_snapshot.clear()

        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)

        safe_extract(zip_file, extract_dir, valid_files)

        # Apply VPP plugin conf from upload (copy if present, delete if not)
        apply_vpp_plugin_conf(extract_dir)

        # Persistent storage settings, same present/absent contract as the VPP
        # conf above: the project owns them, so an upload that carries
        # retain.conf installs it and one that does not removes the device's
        # copy. That absent case is what lets a target whose VPP owns retention
        # switch the built-in file store off simply by not configuring it.
        #
        # Nothing clears retained VALUES here. The store itself decides, at
        # program start, whether what it holds belongs to the program now
        # running — it compares the program MD5 it stored against the one the
        # runtime hands it. Doing it there rather than here is what makes the
        # two platforms behave identically: baremetal has no webserver to
        # observe an upload, and a device flashed or provisioned by any other
        # route still reaches the right answer.
        apply_retain_conf(extract_dir)

        # Update built-in plugin configurations based on extracted config files
        update_plugin_configurations(extract_dir)

        # ?clean=1 — wired from the editor's "Clean build and upload" UI
        # option. Forces a full recompile by wiping core/build/ and the
        # ccache contents before invoking compile.sh. Older editors
        # don't pass this flag, so behaviour for them is unchanged.
        clean_build = flask.request.args.get("clean") == "1"

        # Stage the snapshot only once the program itself is safely in place.
        # run_compile's `finally` is what promotes or discards a staged
        # snapshot, so staging before the extract would leave one stranded if
        # the extract threw -- the compile thread never starts, nothing
        # discards it, and the NEXT successful build would promote a snapshot
        # belonging to an upload that never landed. The clear() above has
        # already erased the old one either way, which is correct: the program
        # it described is gone.
        snapshot_error = stage_project_snapshot()

        # Start compilation in a separate thread
        build_state.status = BuildStatus.COMPILING

        task_compile = threading.Thread(
            target=run_compile,
            args=(runtime_manager,),
            kwargs={"cwd": extract_dir, "clean": clean_build},
            daemon=True,
        )

        task_compile.start()

        # The program upload itself succeeded. A snapshot that could not be
        # stored is reported alongside rather than as a failure: the device is
        # running the new program either way, and failing the upload over the
        # optional half of it would be worse than losing retrievability.
        return {
            "UploadFileFail": "",
            "CompilationStatus": build_state.status.name,
            "ProjectSnapshotWarning": snapshot_error,
        }

    except (OSError, IOError) as e:
        build_state.status = BuildStatus.FAILED
        build_state.log(f"[ERROR] File system error: {e}")
        return {
            "UploadFileFail": f"File system error: {e}",
            "CompilationStatus": build_state.status.name,
        }
    except Exception as e:
        build_state.status = BuildStatus.FAILED
        build_state.log(f"[ERROR] Unexpected error: {e}")
        return {
            "UploadFileFail": f"Unexpected error: {e}",
            "CompilationStatus": build_state.status.name,
        }


def handle_plugin_command(data: dict) -> dict:
    plugin_name = data.get("plugin")
    command = data.get("command")
    params = data.get("params", {})

    if not plugin_name or not command:
        return {"error": "Missing 'plugin' or 'command'"}

    command_json = json.dumps({"command": command, "params": params})
    return runtime_manager.send_plugin_command(plugin_name, command_json)


POST_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "upload-file": handle_upload_file,
    "plugin-command": handle_plugin_command,
}


def restapi_callback_post(argument: str, data: dict) -> dict:
    """
    Dispatch POST callbacks by argument.
    """
    # logger.debug("POST | Received argument: %s, data: %s", argument, data)
    handler = POST_HANDLERS.get(argument)

    if not handler:
        return {"PostRequestError": "Unknown argument"}

    return handler(data)


def run_https():
    # rest api register
    app_restapi.register_blueprint(restapi_bp, url_prefix="/api")
    app_restapi.register_blueprint(discovery_bp)
    # Carries its own /api prefix, like discovery_bp. Its /api/device-info rule
    # is static, so Werkzeug matches it ahead of restapi_bp's /api/<command>
    # catch-all regardless of registration order.
    app_restapi.register_blueprint(runtime_info_bp)
    register_callback_get(restapi_callback_get)
    register_callback_post(restapi_callback_post)

    socketio = init_debug_websocket(app_restapi, runtime_manager.runtime_socket)

    with app_restapi.app_context():
        try:
            db.create_all()
            # Bring a pre-RBAC database up to the current schema (adds the
            # users.role column in place; no-op once present).
            apply_user_schema_migrations()
            db.session.commit()
            # Rescue a device left with accounts but no admin. Unlike the
            # schema migration above this is a DATA repair, and it has to run
            # separately: the migration only fires when the role column is
            # missing, so a database that already has one keeps whatever values
            # it holds -- including none of them being 'admin'. Without an
            # admin there is no API path back to having one.
            repair_missing_admin()
            # logger.info("Database tables created successfully.")
        except Exception:
            # logger.error("Error creating database tables: %s", e)
            pass

    # On non-Linux platforms (MSYS2/Cygwin), patch Python SSL recv socket
    # to handle EAGAIN/EWOULDBLOCK errors that cause "Resource temporarily unavailable"
    is_linux = platform.system() == "Linux"
    if not is_linux:
        logger.info(f"Non-Linux platform detected ({platform.system()}). Patching recv socket...")
        _orig_recv = ssl.SSLSocket.recv

        def _patched_recv(self, buflen, flags=0):
            try:
                return _orig_recv(self, buflen, flags)
            except BlockingIOError as e:
                # Only swallow EAGAIN / EWOULDBLOCK (errno 11) - re-raise other errors
                if getattr(e, "errno", None) in (errno.EAGAIN, errno.EWOULDBLOCK, 11):
                    return b""
                raise

        ssl.SSLSocket.recv = _patched_recv

    try:
        cert_gen = CertGen(hostname=HOSTNAME, ip_addresses=["127.0.0.1"])

        # Check if certificate exists. If not, generate one
        if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
            # logger.info("Generating https certificate...")
            logger.info("Generating https certificate...")
            cert_gen.generate_self_signed_cert(cert_file=CERT_FILE, key_file=KEY_FILE)
        else:
            logger.warning("Credentials already generated!")

        context = (CERT_FILE, KEY_FILE)
        socketio.run(
            app_restapi,
            debug=False,
            host="0.0.0.0",
            port=8443,
            ssl_context=context,
            use_reloader=False,
            log_output=False,
            allow_unsafe_werkzeug=True,
        )

    except FileNotFoundError:
        # logger.error("Could not find SSL credentials! %s", e)
        pass
    except ssl.SSLError:
        # logger.error("SSL credentials FAIL! %s", e)
        pass
    except KeyboardInterrupt:
        # logger.info("HTTP server stopped by KeyboardInterrupt")
        pass
    finally:
        logger.info("Runtime manager stopped")
        runtime_manager.stop()
        network_discovery_responder.stop()


if __name__ == "__main__":
    run_https()
