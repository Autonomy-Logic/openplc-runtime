import asyncio
import logging
import os
import sqlite3
import ssl
import threading
from pathlib import Path
from typing import Callable
import time
import zipfile
import sys
import shutil

import flask
import flask_login
import openplc
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

app = flask.Flask(__name__)
app.secret_key = str(os.urandom(16))
login_manager = flask_login.LoginManager()
login_manager.init_app(app)

logger = logging.getLogger(__name__)

openplc_runtime = openplc.runtime()
client = SyncUnixClient("/run/runtime/plc_runtime.socket")
client.connect()

log_server = UnixLogServer("/run/runtime/log_runtime.socket")
log_server.start()

BASE_DIR = Path(__file__).parent
CERT_FILE = (BASE_DIR / "certOPENPLC.pem").resolve()
KEY_FILE = (BASE_DIR / "keyOPENPLC.pem").resolve()
HOSTNAME = "localhost"

MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB per file
MAX_TOTAL_SIZE = 50 * 1024 * 1024  # 50 MB total
DISALLOWED_EXT = {".exe", ".dll", ".sh", ".bat", ".js", ".vbs", ".scr"}


def analyze_zip(zip_path) -> (bool, list):
    if not zipfile.is_zipfile(zip_path):
        logger.warning("Not a valid ZIP file.")
        return False, []

    with zipfile.ZipFile(zip_path, "r") as zf:
        total_size = 0
        safe = True
        valid_files = []

        for info in zf.infolist():
            filename = info.filename
            uncompressed_size = info.file_size
            compressed_size = info.compress_size
            ext = os.path.splitext(filename)[1].lower()

            # Check for path traversal or absolute paths
            if filename.startswith("/") or ".." in filename or ":" in filename:
                logger.warning(f"Dangerous path: {filename}")
                safe = False

            # Check uncompressed size
            if uncompressed_size > MAX_FILE_SIZE:
                logger.warning(f"File too large: {filename} ({uncompressed_size} bytes)")
                safe = False

            # Check compression ratio (ZIP bomb detection)
            if compressed_size > 0 and uncompressed_size / compressed_size > 1000:
                logger.warning(f"Suspicious compression ratio in {filename}")
                safe = False

            # Check disallowed extensions
            if ext in DISALLOWED_EXT:
                logger.warning(f"Disallowed extension: {filename}")
                safe = False

            total_size += uncompressed_size
            valid_files.append(info)

        # Check total size
        if total_size > MAX_TOTAL_SIZE:
            logger.warning(f"Total uncompressed size too large: {total_size} bytes")
            safe = False

        if safe:
            logger.info("ZIP file looks safe to extract (based on static checks).")
        else:
            logger.warning("ZIP file failed safety checks.")

        return safe, valid_files


def safe_extract(zip_path, dest_dir, valid_files):
    """Extract files safely to a target directory."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in valid_files:
            filename = info.filename

            # Skip directory entries
            if filename.endswith("/"):
                dir_path = os.path.join(dest_dir, filename)
                os.makedirs(dir_path, exist_ok=True)
                continue

            out_path = os.path.join(dest_dir, filename)
            out_path = os.path.abspath(out_path)

            # Ensure extraction stays inside destination
            if not out_path.startswith(os.path.abspath(dest_dir)):
                print(f"⚠️ Skipping suspicious path: {filename}")
                continue

            # Create parent directories if needed
            os.makedirs(os.path.dirname(out_path), exist_ok=True)

            # Extract file
            with zf.open(info) as src, open(out_path, "wb") as dst:
                dst.write(src.read())

            logger.info(f"Extracted: {out_path}")


def create_connection(db_file):
    """ Create a connection to the database file """
    try:
        conn = sqlite3.connect(db_file)
        return conn
    except sqlite3.Error as e:
        logger.error("Error creating database connection: %s", e)

    return None


def handle_start_plc(data: dict) -> dict:
    response = client.start_plc()
    return {"status": response}


def handle_stop_plc(data: dict) -> dict:
    response = client.stop_plc()
    return {"status": response}


def handle_runtime_logs(data: dict) -> dict:
    return {"runtime-logs": list(log_server.log_buffer)}


def handle_compilation_status(data: dict) -> dict:
    try:
        logs = openplc_runtime.compilation_status()
        _logs = logs
    except Exception as e:
        logger.error("Error retrieving compilation logs: %s", e)
        _logs = str(e)

    status = _logs
    if not isinstance(status, str):
        _status = "No compilation in progress"
        _error = ""
    elif "Compilation finished successfully!" in status:
        _status = "Success"
        _error = "No error"
    elif "Compilation finished with errors!" in status:
        _status = "Error"
        _error = openplc_runtime.get_compilation_error()
    else:
        _status = "Compiling"
        _error = openplc_runtime.get_compilation_error()
    
    logger.debug(
        "Compilation status: %s, logs: %s", _status, _logs, extra={"error": _error}
    )

    return {"status": _status, "logs": _logs, "error": _error}


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
    filename = None

    # Validate file presence
    if "file" not in flask.request.files:
        return {"UploadFileFail": "No file part in the request"}
    
    zip_file = flask.request.files["file"]

    if zip_file.content_length > MAX_FILE_SIZE:
        return {"UploadFileFail": "File is too large"}

    safe, valid_files = analyze_zip(zip_file)

    if not safe:
        return {"UploadFileFail": "Uploaded ZIP file failed safety checks"}

    # delete directory generated 
    extract_dir = "core/generated"
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)

    # recreate directory
    # os.makedirs(extract_dir, exist_ok=True)
    # extract files
    safe_extract(zip_file, extract_dir, valid_files)

    return {"UploadFile": safe}

    # # Database operations
    # database = "openplc.db"
    # conn = create_connection(database)
    # if conn is None:
    #     return {"UploadFileFail": "Error connecting to the database"}
    
    # logger.info("%s connected", database)
    
    # try:
    #     cur = conn.cursor()
    #     cur.execute("SELECT * FROM Programs WHERE Name = 'webserver_program'")
    #     row = cur.fetchone()
        
    #     if not row or len(row) < 4:
    #         return {"UploadFileFail": "Program record not found or invalid"}
        
    #     filename = str(row[3])
    #     st_file.save(f"st_files/{filename}")
    #     cur.close()
        
    # except Exception as e:
    #     return {"UploadFileFail": f"Database operation failed: {e}"}
    # finally:
    #     if conn:
    #         conn.close()

    # if openplc_runtime.status() == "Compiling":
    #     return {"RuntimeStatus": "Compiling"}

    # try:
    #     openplc_runtime.compile_program(filename)
    #     return {"CompilationStatus": "Starting program compilation"}
    # except Exception as e:
    #     return {"CompilationStatusFail": f"Compilation failed: {e}"}


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
    finally:
        openplc_runtime.stop_runtime()


if __name__ == "__main__":
    run_https()
