"""Behavioural tests for `RuntimeManager.clear_retained()` and `retain_status()`.

`clear_retained` runs on every program upload, and it is the one thing standing
between a new program and the previous program's retained values. Two properties
matter and neither is obvious from reading it:

  * it must never break an upload — the upload is what the user asked for, and a
    runtime that cannot be reached has nothing stored that this program will
    read anyway;
  * it must not report success when it did not happen, because "cleared" is what
    the caller writes to the log.
"""

import socket
import sys
import types
from pathlib import Path

import pytest

# `webserver.runtimemanager` pulls in the whole webserver package at import
# time; the socket is the only collaborator these tests care about.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from webserver.runtimemanager import RuntimeManager  # noqa: E402


class FakeSocket:
    """Stands in for the runtime control socket."""

    def __init__(self, reply=None, raises=None):
        self.reply = reply
        self.raises = raises
        self.sent = []

    def send_and_receive(self, message):
        self.sent.append(message)
        if self.raises is not None:
            raise self.raises
        return self.reply


def manager(sock):
    """A RuntimeManager with only its socket wired — no threads, no daemon."""
    mgr = RuntimeManager.__new__(RuntimeManager)
    mgr.runtime_socket = sock
    return mgr


# --- clear_retained -------------------------------------------------------


def test_sends_the_clear_command():
    sock = FakeSocket(reply="RETAIN:OK\n")
    assert manager(sock).clear_retained() == "RETAIN:OK\n"
    assert sock.sent == ["RETAIN:CLEAR\n"]


@pytest.mark.parametrize(
    "failure",
    [
        OSError("no such socket"),
        socket.error("connection reset"),
        RuntimeError("something unexpected"),
    ],
)
def test_a_failure_never_propagates_into_the_upload(failure):
    # An upload must not fail because retained values could not be discarded.
    sock = FakeSocket(raises=failure)
    assert manager(sock).clear_retained() == "RETAIN:ERROR\n"


def test_a_failure_is_reported_as_error_not_as_ok():
    # The distinction the caller logs. Swallowing the exception AND answering OK
    # would say "cleared" about a device that still holds the old values.
    sock = FakeSocket(raises=OSError("down"))
    assert "ERROR" in manager(sock).clear_retained()


# --- retain_status --------------------------------------------------------


def test_reports_the_live_backend():
    sock = FakeSocket(reply="RETAIN:STATUS active plugin synergy\n")
    assert manager(sock).retain_status() == {
        "active": True,
        "backend": "plugin",
        "detail": "synergy",
    }


def test_reports_the_file_backend_with_its_path():
    sock = FakeSocket(reply="RETAIN:STATUS active file /var/lib/openplc-runtime/retain.bin\n")
    status = manager(sock).retain_status()
    assert status["backend"] == "file"
    assert status["detail"] == "/var/lib/openplc-runtime/retain.bin"


def test_reports_inactive_without_a_detail():
    sock = FakeSocket(reply="RETAIN:STATUS inactive none\n")
    assert manager(sock).retain_status() == {"active": False, "backend": "none", "detail": ""}


@pytest.mark.parametrize(
    "reply",
    ["", "RETAIN:OK\n", "garbage\n", "RETAIN:STATUS active\n", None],
)
def test_an_unreadable_reply_is_unknown_rather_than_a_guess(reply):
    # A runtime too old to answer, or a truncated reply, must not be reported as
    # "no retention configured" — the Persistent Storage screen would then tell
    # the operator something it does not know.
    assert manager(FakeSocket(reply=reply)).retain_status()["backend"] == "unknown"


def test_an_unreachable_runtime_is_unknown():
    assert manager(FakeSocket(raises=OSError("down"))).retain_status() == {
        "active": False,
        "backend": "unknown",
        "detail": "",
    }
