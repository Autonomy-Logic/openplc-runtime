# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Sign-in sessions, token rotation, persistent revocation and login throttling.

Every sign-in starts a session; its tokens carry the session id (``sid``) and
the issue time in milliseconds (``iat_ms``). Renewing rotates the token: the
old one is recorded as revoked and refused from then on, and presenting it
again after ``ROTATION_GRACE_S`` ends the whole session, since only a copy can
still hold it. Sign-out ends one session; an account-wide end (password, role
or deletion) is a cut-off time on the account that both this process and the
Go bootloader read. Everything lives in the runtime database, so it survives
restarts.

Failed sign-ins are throttled per client address only, whatever the username:
after a failure the next attempt from that address waits
``THROTTLE_FIRST_DELAY_S``, doubling with each further failure up to
``THROTTLE_MAX_DELAY_S``. Failures from one address never delay a sign-in from
another, and a success does not clear the counter.
"""

import math
import secrets
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from webserver.database import db
from webserver.logger import get_logger

logger, _ = get_logger("logger", use_buffer=True)

TOKEN_LIFETIME = timedelta(minutes=15)
ROTATION_GRACE_S = 10.0
THROTTLE_FIRST_DELAY_S = 5.0
THROTTLE_MAX_DELAY_S = 300.0
# A counter whose last failure is older than this starts again from the first delay.
THROTTLE_FORGET_S = 3600.0
# Counter shared by every sign-in whose client address is unknown.
UNKNOWN_ADDRESS_KEY = "addr:unknown"
# A session not renewed for this long is dropped even if it was never ended.
IDLE_SESSION_PURGE_S = 86400.0
# Password checks from one client address run one at a time, under one of these locks.
_THROTTLE_LOCKS = tuple(threading.Lock() for _ in range(64))
# Called with the jti of a rotated token and its successor, e.g. to re-key open debug sockets.
_rotation_listeners: list[Callable[[str, str], None]] = []

SID_CLAIM = "sid"
IAT_MS_CLAIM = "iat_ms"


class AuthSession(db.Model):  # type: ignore[name-defined]
    """One sign-in, kept until its last token expires."""

    __tablename__ = "auth_sessions"
    sid: str = db.Column(db.Text, primary_key=True)
    user_id: int = db.Column(db.Integer, nullable=False, index=True)
    created_at: float = db.Column(db.Float, nullable=False)
    expires_at: float = db.Column(db.Float, nullable=False)
    revoked_at: float | None = db.Column(db.Float, nullable=True)
    revoked_reason: str | None = db.Column(db.Text, nullable=True)


class RevokedToken(db.Model):  # type: ignore[name-defined]
    """A token refused before its expiry: rotated away or signed out."""

    __tablename__ = "revoked_tokens"
    jti: str = db.Column(db.Text, primary_key=True)
    sid: str = db.Column(db.Text, nullable=False)
    revoked_at: float = db.Column(db.Float, nullable=False)
    expires_at: float = db.Column(db.Float, nullable=False)
    reason: str = db.Column(db.Text, nullable=False)


class LoginThrottle(db.Model):  # type: ignore[name-defined]
    """Failed sign-ins from one client address."""

    __tablename__ = "login_throttle"
    key: str = db.Column(db.Text, primary_key=True)
    failures: int = db.Column(db.Integer, nullable=False, default=0)
    last_failure_at: float = db.Column(db.Float, nullable=False)
    next_allowed_at: float = db.Column(db.Float, nullable=False)


@dataclass(frozen=True)
class TokenRefusal:
    """Why a token is refused; ``log`` says whether the refusal is worth a security log line."""

    reason: str
    log: bool


def now_ms() -> int:
    """Milliseconds since the epoch."""
    return int(time.time() * 1000)


def start_session(user_id: int, issued_ms: int) -> dict[str, object]:
    """Starts a session for ``user_id``; ``issued_ms`` is when its sign-in began."""
    now = time.time()
    sid = secrets.token_urlsafe(24)
    db.session.add(
        AuthSession(
            sid=sid,
            user_id=user_id,
            created_at=now,
            expires_at=now + TOKEN_LIFETIME.total_seconds(),
        )
    )
    db.session.commit()
    return {SID_CLAIM: sid, IAT_MS_CLAIM: issued_ms}


def check_token(payload: dict[str, object], valid_after_ms: int) -> TokenRefusal | None:
    """None when the token ``payload`` is in force; ``valid_after_ms`` is the account cut-off."""
    sid = payload.get(SID_CLAIM)
    jti = payload.get("jti")
    if not isinstance(sid, str) or not isinstance(jti, str):
        return TokenRefusal("token without a session", log=False)
    revoked = db.session.get(RevokedToken, jti)
    if revoked is not None:
        if revoked.reason == "rotated" and time.time() - revoked.revoked_at > ROTATION_GRACE_S:
            end_session(sid, "a renewed token was used again")
        return TokenRefusal(f"token {revoked.reason}", log=revoked.reason == "rotated")
    session = db.session.get(AuthSession, sid)
    if session is None or session.revoked_at is not None:
        return TokenRefusal("session ended", log=False)
    issued = payload.get(IAT_MS_CLAIM)
    if not isinstance(issued, int) or (valid_after_ms > 0 and issued <= valid_after_ms):
        return TokenRefusal("account sessions ended", log=False)
    return None


def rotate(payload: dict[str, object]) -> dict[str, object] | None:
    """Revokes the token ``payload`` as rotated; its successor's claims, None if already rotated."""
    sid = str(payload[SID_CLAIM])
    now = time.time()
    db.session.add(
        RevokedToken(
            jti=str(payload["jti"]),
            sid=sid,
            revoked_at=now,
            expires_at=_expiry(payload, now),
            reason="rotated",
        )
    )
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return None
    session = db.session.get(AuthSession, sid)
    if session is not None:
        session.expires_at = now + TOKEN_LIFETIME.total_seconds()
    db.session.commit()
    return {SID_CLAIM: sid, IAT_MS_CLAIM: now_ms()}


def on_rotation(listener: Callable[[str, str], None]) -> None:
    """Registers ``listener(old_jti, new_token)``, called after each successful renewal."""
    _rotation_listeners.append(listener)


def notify_rotated(old_jti: str, new_token: str) -> None:
    """Tells the listeners a token was renewed."""
    for listener in _rotation_listeners:
        listener(old_jti, new_token)


def sign_out(payload: dict[str, object]) -> None:
    """Ends the session of the token ``payload`` and revokes the token."""
    now = time.time()
    sid = str(payload.get(SID_CLAIM, ""))
    db.session.merge(
        RevokedToken(
            jti=str(payload["jti"]),
            sid=sid,
            revoked_at=now,
            expires_at=_expiry(payload, now),
            reason="signed-out",
        )
    )
    db.session.commit()
    end_session(sid, "signed out")


def end_session(sid: str, reason: str) -> None:
    """Ends the session ``sid``, logging why."""
    session = db.session.get(AuthSession, sid)
    if session is None or session.revoked_at is not None:
        return
    session.revoked_at = time.time()
    session.revoked_reason = reason
    db.session.commit()
    logger.warning("Session of account %s ended: %s", session.user_id, reason)


def end_account_sessions(user_id: int, reason: str) -> int:
    """Ends every open session of ``user_id``; returns the cut-off to store on the account."""
    cutoff = now_ms()
    now = time.time()
    AuthSession.query.filter_by(user_id=user_id, revoked_at=None).update(
        {"revoked_at": now, "revoked_reason": reason}
    )
    db.session.commit()
    logger.warning("Every session of account %s ended: %s", user_id, reason)
    return cutoff


def purge(now: float | None = None) -> None:
    """Drops expired revocations, ended sessions past expiry, and sessions idle for a day."""
    at = time.time() if now is None else now
    RevokedToken.query.filter(RevokedToken.expires_at < at).delete()
    AuthSession.query.filter(
        db.or_(
            db.and_(AuthSession.revoked_at.isnot(None), AuthSession.expires_at < at),
            AuthSession.expires_at < at - IDLE_SESSION_PURGE_S,
        )
    ).delete(synchronize_session=False)
    LoginThrottle.query.filter(LoginThrottle.last_failure_at < at - THROTTLE_FORGET_S).delete()
    db.session.commit()


def _expiry(payload: dict[str, object], now: float) -> float:
    exp = payload.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else now + TOKEN_LIFETIME.total_seconds()


@contextmanager
def throttle_lock(key: str) -> Iterator[None]:
    """Serializes password checks on ``key``, so none skips a wait."""
    lock = _THROTTLE_LOCKS[zlib.crc32(key.encode()) % len(_THROTTLE_LOCKS)]
    with lock:
        yield


def throttle_key(address: str | None) -> str:
    """The counter of a sign-in from client ``address``; one shared counter when it is unknown."""
    return f"addr:{address}" if address else UNKNOWN_ADDRESS_KEY


def throttle_wait(key: str, now: float | None = None) -> int:
    """Whole seconds before the next attempt on ``key`` is accepted; 0 when it may go ahead."""
    at = time.time() if now is None else now
    row = db.session.get(LoginThrottle, key)
    if row is None or row.next_allowed_at <= at:
        return 0
    return math.ceil(row.next_allowed_at - at)


def record_failure(key: str, now: float | None = None) -> None:
    """Counts a failed sign-in on ``key`` and sets its next allowed attempt."""
    at = time.time() if now is None else now
    row = db.session.get(LoginThrottle, key)
    if row is None:
        row = LoginThrottle(key=key, failures=0, last_failure_at=at, next_allowed_at=at)
        db.session.add(row)
    elif at - row.last_failure_at > THROTTLE_FORGET_S:
        row.failures = 0
    row.failures += 1
    row.last_failure_at = at
    row.next_allowed_at = at + throttle_delay(row.failures)
    db.session.commit()


def throttle_delay(failures: int) -> float:
    """Seconds an attempt waits after ``failures`` consecutive failures."""
    if failures <= 0:
        return 0.0
    return min(THROTTLE_FIRST_DELAY_S * 2 ** min(failures - 1, 16), THROTTLE_MAX_DELAY_S)
