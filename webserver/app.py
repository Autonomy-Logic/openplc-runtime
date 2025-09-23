import logging
import os
import ssl
from pathlib import Path
from typing import Callable
import shutil
from typing import Final

import flask
import flask_login
from credentials import CertGen
from restapi import (
    app_restapi,
    db,
    register_callback_get,
    register_callback_post,
    restapi_bp,
)
from unixclient import SyncUnixClient
from unixserver import UnixLogServer
from plcapp_management import (build_state, BuildStatus,
                               analyze_zip, run_compile, safe_extract,
                               MAX_FILE_SIZE, MAX_TOTAL_SIZE, DISALLOWED_EXT)

app = flask.Flask(__name__)
app.secret_key = str(os.urandom(16))
login_manager = flask_login.LoginManager()
login_manager.init_app(app)

logger = logging.getLogger(__name__)

client = SyncUnixClient("/run/runtime/plc_runtime.socket")
client.connect()

log_server = UnixLogServer("/run/runtime/log_runtime.socket")
log_server.start()

BASE_DIR: Final[Path] = Path(__file__).parent
CERT_FILE: Final[Path] = (BASE_DIR / "certOPENPLC.pem").resolve()
KEY_FILE: Final[Path] = (BASE_DIR / "keyOPENPLC.pem").resolve()
HOSTNAME: Final[str] = "localhost"


def handle_start_plc(data: dict) -> dict:
    response = client.start_plc()
    return {"status": response}


def handle_stop_plc(data: dict) -> dict:
    response = client.stop_plc()
    return {"status": response}


def handle_runtime_logs(data: dict) -> dict:
    return {"runtime-logs": list(log_server.log_buffer)}


def handle_compilation_status(data: dict) -> dict:
    return {
        "status": build_state.status.name,
        "logs": build_state.logs[-20:],  # last 20 lines
        "exit_code": build_state.exit_code
    }

def handle_status(data: dict) -> dict:
    return {"current_status": "operational", "details": data}


def handle_ping(data: dict) -> dict:
    response = client.ping()
    return {"status": response}


GET_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "start-plc": handle_start_plc,
    "stop-plc": handle_stop_plc,
    "runtime-logs": handle_runtime_logs,
    "compilation-status": handle_compilation_status,
    "status": handle_status,
    "ping": handle_ping,
}


def restapi_callback_get(argument: str, data: dict) -> dict:
    """
    Dispatch GET callbacks by argument.
    """
    logger.debug("GET | Received argument: %s, data: %s", argument, data)
    handler = GET_HANDLERS.get(argument)
    if handler:
        return handler(data)
    return {"error": "Unknown argument"}


def handle_upload_file(data: dict) -> dict:
    if build_state.status == BuildStatus.COMPILING:
        return {"CompilationStatus": "Program is compiling, please wait"}
    
    if "file" not in flask.request.files:
        return {"UploadFileFail": "No file part in the request"}
    
    zip_file = flask.request.files["file"]

    if zip_file.content_length > MAX_FILE_SIZE:
        return {"UploadFileFail": "File is too large"}

    safe, valid_files = analyze_zip(zip_file)
    if not safe:
        return {"UploadFileFail": "Uploaded ZIP file failed safety checks"}

    extract_dir = "core/generated"
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)

    safe_extract(zip_file, extract_dir, valid_files)
    run_compile(client=client, cwd=extract_dir)

    # while build_state.status == BuildStatus.COMPILING:
    if build_state.status == BuildStatus.SUCCESS:
        return {"CompilationStatus": build_state.status.name}
    elif build_state.status == BuildStatus.FAILED:
        return {"CompilationStatus":
                f"Compilation failed:\n{build_state.logs[-1]}"}

    return {"UploadFileFail": "Unknown error during file upload"}


POST_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "upload-file": handle_upload_file,
}


def restapi_callback_post(argument: str, data: dict) -> dict:
    """
    Dispatch POST callbacks by argument.
    """
    logger.debug("POST | Received argument: %s, data: %s", argument, data)
    handler = POST_HANDLERS.get(argument)
    
    if not handler:
        return {"PostRequestError": "Unknown argument"}
    
    return handler(data)

def run_https():
    # rest api register
    app_restapi.register_blueprint(restapi_bp, url_prefix="/api")
    register_callback_get(restapi_callback_get)
    register_callback_post(restapi_callback_post)

    with app_restapi.app_context():
        try:
            db.create_all()
            db.session.commit()
            logger.info("Database tables created successfully.")
        except Exception as e:
            logger.error("Error creating database tables: %s", e)

    try:
        cert_gen = CertGen(hostname=HOSTNAME, ip_addresses=["127.0.0.1"])
        if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
            cert_gen.generate_self_signed_cert(cert_file=CERT_FILE, 
                                               key_file=KEY_FILE)
        elif cert_gen.is_certificate_valid(CERT_FILE):
            cert_gen.generate_self_signed_cert(cert_file=CERT_FILE, key_file=KEY_FILE)
        else:
            print("Credentials already generated!")

        context = (CERT_FILE, KEY_FILE)
        app_restapi.run(
            debug=False,
            host="0.0.0.0",
            threaded=True,
            port=8443,
            ssl_context=context,
        )

    except FileNotFoundError as e:
        logger.error("Could not find SSL credentials! %s", e)
    except ssl.SSLError as e:
        logger.error("SSL credentials FAIL! %s", e)
    except KeyboardInterrupt:
        logger.info("HTTP server stopped by KeyboardInterrupt")


if __name__ == "__main__":
    run_https()
