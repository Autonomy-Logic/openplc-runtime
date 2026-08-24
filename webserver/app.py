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

from webserver.credentials import CertGen
from webserver.debug_websocket import init_debug_websocket
from webserver.discovery.discovery_routes import discovery_bp
from webserver.discovery.network_discovery import responder as network_discovery_responder
from webserver.logger import get_logger
from webserver.plcapp_management import (
    MAX_FILE_SIZE,
    BuildStatus,
    analyze_zip,
    build_state,
    run_compile,
    safe_extract,
    apply_vpp_plugin_conf,
    update_plugin_configurations,
)
from webserver.restapi import (
    app_restapi,
    apply_user_schema_migrations,
    db,
    register_callback_get,
    register_callback_post,
    restapi_bp,
)
from webserver.runtimemanager import RuntimeManager
from webserver.vpp_package_signature import (
    verify_uploaded_vpp_plugin,
    write_verification_seal,
)

logger, _ = get_logger("logger", use_buffer=True)

app = flask.Flask(__name__)
app.secret_key = str(os.urandom(16))
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
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)

        safe_extract(zip_file, extract_dir, valid_files)

        # A new program must not inherit the previous one's retained values.
        # The layout hash already refuses a genuinely different set of retained
        # variables, but two programs that happen to share a layout would
        # otherwise silently inherit each other's state. CODESYS clears retained
        # memory on download; this is the same rule.
        try:
            runtime_manager.clear_retained()
        except Exception as e:  # never fail an upload over this
            logger.warning("Could not clear retained values before upload: %s", e)

        # Verify the VPP package signature BEFORE anything from this upload is
        # copied into the runtime root and before any Makefile from it runs.
        #
        # The order matters and is the reason this sits here rather than in
        # compile.sh: a licensed VPP ships the closed enforcement objects and a
        # link-only Makefile inside the upload, and compile.sh runs that
        # Makefile as root. Refusing here means an unsigned or tampered plugin
        # never gets a vpp_plugins.conf installed, never gets its config or
        # license blob copied out, and never reaches `make`.
        #
        # Policy for uploads without a signature lives in exactly one place --
        # vpp_package_signature.signature_required(). Today: only uploads that
        # carry a vpp_plugin/ directory must be signed, so plain PLC programs
        # from any editor keep working untouched.
        verification = verify_uploaded_vpp_plugin(extract_dir)
        if not verification.ok:
            build_state.status = BuildStatus.FAILED
            build_state.log(f"[ERROR] {verification.error}\n")
            return {
                "UploadFileFail": verification.error,
                "CompilationStatus": build_state.status.name,
            }
        write_verification_seal(extract_dir, verification)

        # Apply VPP plugin conf from upload (copy if present, delete if not)
        apply_vpp_plugin_conf(extract_dir)

        # Update built-in plugin configurations based on extracted config files
        update_plugin_configurations(extract_dir)

        # ?clean=1 — wired from the editor's "Clean build and upload" UI
        # option. Forces a full recompile by wiping core/build/ and the
        # ccache contents before invoking compile.sh. Older editors
        # don't pass this flag, so behaviour for them is unchanged.
        clean_build = flask.request.args.get("clean") == "1"

        # Start compilation in a separate thread
        build_state.status = BuildStatus.COMPILING

        task_compile = threading.Thread(
            target=run_compile,
            args=(runtime_manager,),
            kwargs={"cwd": extract_dir, "clean": clean_build},
            daemon=True,
        )

        task_compile.start()

        return {"UploadFileFail": "", "CompilationStatus": build_state.status.name}

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
