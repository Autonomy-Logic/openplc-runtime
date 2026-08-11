"""Minimal stand-in for the webserver's log server.

plc_main's log_init() connects to /run/runtime/log_runtime.socket; with nothing
listening, --print-logs produces nothing on stdout either, which is how a whole
round of testing ended up with empty journals.
"""

import os
import socket
import threading

PATH = "/run/runtime/log_runtime.socket"

try:
    os.unlink(PATH)
except OSError:
    pass

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(PATH)
srv.listen(16)


def drain(conn):
    with conn:
        while True:
            try:
                if not conn.recv(8192):
                    return
            except OSError:
                return


while True:
    try:
        c, _ = srv.accept()
    except OSError:
        break
    threading.Thread(target=drain, args=(c,), daemon=True).start()
