import asyncio
import logging
import os
import sqlite3
import ssl
import threading
from pathlib import Path
from typing import Callable
import time

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
from runtimemanager import RuntimeManager


app = flask.Flask(__name__)
app.secret_key = str(os.urandom(16))
login_manager = flask_login.LoginManager()
login_manager.init_app(app)

logger = logging.getLogger(__name__)

openplc_runtime = openplc.runtime()
runtime_manager = RuntimeManager(
    runtime_path="./build/plc_main",
    plc_socket="/run/runtime/plc_runtime.socket",
    log_socket="/run/runtime/log_runtime.socket",
)

runtime_manager.start()

BASE_DIR = Path(__file__).parent
CERT_FILE = (BASE_DIR / "certOPENPLC.pem").resolve()
KEY_FILE = (BASE_DIR / "keyOPENPLC.pem").resolve()
HOSTNAME = "localhost"

def create_connection(db_file):
    """ Create a connection to the database file """
    try:
        conn = sqlite3.connect(db_file)
        return conn
    except sqlite3.Error as e:
        logger.error("Error creating database connection: %s", e)

    return None


def handle_start_plc(data: dict) -> dict:
    response = runtime_manager.start_plc()
    return {"status": response}


def handle_stop_plc(data: dict) -> dict:
    response = runtime_manager.stop_plc()
    return {"status": response}


def handle_runtime_logs(data: dict) -> dict:
    response = runtime_manager.get_logs()
    return {"runtime-logs": response}


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
    response = runtime_manager.ping()
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
    
    st_file = flask.request.files["file"]
    
    if st_file.content_length > 32 * 1024 * 1024:  # 32 MB limit
        return {"UploadFileFail": "File is too large"}

    # Database operations
    database = "openplc.db"
    conn = create_connection(database)
    if conn is None:
        return {"UploadFileFail": "Error connecting to the database"}
    
    logger.info("%s connected", database)
    
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM Programs WHERE Name = 'webserver_program'")
        row = cur.fetchone()
        
        if not row or len(row) < 4:
            return {"UploadFileFail": "Program record not found or invalid"}
        
        filename = str(row[3])
        st_file.save(f"st_files/{filename}")
        cur.close()
        
    except Exception as e:
        return {"UploadFileFail": f"Database operation failed: {e}"}
    finally:
        if conn:
            conn.close()

    if openplc_runtime.status() == "Compiling":
        return {"RuntimeStatus": "Compiling"}

    try:
        openplc_runtime.compile_program(filename)
        return {"CompilationStatus": "Starting program compilation"}
    except Exception as e:
        return {"CompilationStatusFail": f"Compilation failed: {e}"}


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
        runtime_manager.stop()
        logger.info("Runtime manager stopped")


if __name__ == "__main__":
    run_https()
