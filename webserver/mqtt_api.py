# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Authenticated MQTT endpoints, mounted under ``/api/mqtt``.

No endpoint returns a password, private key or ciphertext: credentials are
write-only (stage, cancel, remove) and only their presence and non-secret
metadata can be read.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final

from flask import Blueprint, Response, jsonify, request
from flask_jwt_extended import current_user, get_jwt_identity, jwt_required
from flask_jwt_extended.exceptions import JWTExtendedException
from jwt import PyJWTError

from webserver import mqtt_config

# isort: split
# Importing mqtt_config first puts the MQTT plugin directory on sys.path.
# pylint: disable=wrong-import-order
from contract.runtime_config import is_uuid_v4
from mqtt_credentials import material
from mqtt_credentials.material import CryptoUnavailable
from mqtt_credentials.model import (
    MAX_CERTIFICATE_BYTES,
    MAX_KEY_BYTES,
    MAX_PASSPHRASE_BYTES,
    MAX_PASSWORD_BYTES,
    MAX_REQUEST_BYTES,
    MAX_REQUEST_JSON_BYTES,
    Actor,
    CertificateInfo,
    CredentialError,
    SecretPayload,
    StagingRequest,
    VaultCorrupt,
    parse_staging_request,
)

# pylint: enable=wrong-import-order

mqtt_bp = Blueprint("mqtt_blueprint", __name__)
_plc_status: Callable[[], str] | None = None


def set_plc_status_source(source: Callable[[], str]) -> None:
    """Register how to read the PLC state line (``STATUS:<state>``) of the runtime core."""
    global _plc_status  # pylint: disable=global-statement
    _plc_status = source


def plc_running() -> bool:
    if _plc_status is None:
        return False
    return _plc_status().strip() == "STATUS:RUNNING"


_PART_LIMITS: Final[dict[str, int]] = {
    "request": MAX_REQUEST_JSON_BYTES,
    "password": MAX_PASSWORD_BYTES,
    "client_certificate": MAX_CERTIFICATE_BYTES,
    "client_key": MAX_KEY_BYTES,
    "client_key_passphrase": MAX_PASSPHRASE_BYTES,
}
_PARTS_BY_METHOD: Final[dict[str, tuple[frozenset[str], frozenset[str]]]] = {
    "username_password": (frozenset({"request", "password"}), frozenset()),
    "client_certificate": (
        frozenset({"request", "client_certificate", "client_key"}),
        frozenset({"client_key_passphrase"}),
    ),
}


def request_actor() -> Actor | None:
    """The caller as proven by the JWT the route already verified; None outside one.

    Client-supplied names are never used as identity.
    """
    try:
        identity = get_jwt_identity()
        user = current_user
        username = getattr(user, "username", "") if user is not None else ""
    except (RuntimeError, JWTExtendedException, PyJWTError):
        return None
    if identity is None:
        return None
    return Actor(str(identity), str(username or ""), request.remote_addr or "")


def _error(exc: CredentialError) -> tuple[Response, int]:
    return jsonify(exc.to_api()), exc.status


def _internal() -> tuple[Response, int]:
    return _error(CredentialError("internal_error", "the credential vault cannot be used"))


def _part(name: str) -> bytes | None:
    """One multipart part as bytes, from a file or a plain form field."""
    files = request.files.getlist(name)
    fields = request.form.getlist(name)
    if len(files) + len(fields) > 1:
        raise CredentialError("credential_invalid_format", f"part '{name}' is repeated")
    limit = _PART_LIMITS[name]
    if files:
        data = files[0].read(limit + 1)
    elif fields:
        data = fields[0].encode("utf-8")
    else:
        return None
    if len(data) > limit:
        raise CredentialError("credential_too_large", f"part '{name}' exceeds {limit} bytes")
    return data


def _read_staging() -> tuple[StagingRequest, SecretPayload, CertificateInfo | None]:
    names = set(request.files) | set(request.form)
    unknown = names - set(_PART_LIMITS)
    if unknown:
        raise CredentialError("credential_invalid_format", "the request has unknown parts")
    raw_request = _part("request")
    if raw_request is None:
        raise CredentialError("credential_invalid_format", "the 'request' part is required")
    staging = parse_staging_request(raw_request)
    if staging.method not in mqtt_config.credential_methods():
        raise CredentialError(
            "credential_method_mismatch",
            "this runtime cannot store credentials for this method",
            "/method",
        )
    required, optional = _PARTS_BY_METHOD[staging.method]
    if not required <= names or not names <= required | optional:
        raise CredentialError(
            "credential_invalid_format", "the secret parts do not match the credential method"
        )
    if staging.method == "username_password":
        password = _part("password")
        if not password:
            raise CredentialError("credential_invalid_format", "the password must not be empty")
        return staging, SecretPayload(staging.username, password, None, None), None
    certificate = _part("client_certificate") or b""
    key = _part("client_key") or b""
    passphrase = _part("client_key_passphrase")
    identity = material.validate_client_identity(
        certificate, key, passphrase, time.time_ns() // 1_000_000
    )
    payload = SecretPayload(None, None, identity.certificate_chain_pem, identity.key_pem)
    return staging, payload, identity.info


@mqtt_bp.route("/capabilities", methods=["GET"])
@jwt_required()
def mqtt_capabilities() -> tuple[Response, int]:
    """Report the MQTT contract this runtime accepts.

    Producers decide from this content, never from the runtime version.
    ---
    tags:
      - MQTT
    security:
      - BearerAuth: []
    responses:
      200:
        description: Accepted configuration contract, modes, credential methods and limits
      401:
        description: Authentication required
    """
    return jsonify(mqtt_config.capabilities()), 200


@mqtt_bp.route("/status", methods=["GET"])
@jwt_required()
def mqtt_status() -> tuple[Response, int]:
    """Sanitized state of the MQTT integration: no process values and no secrets.

    Answered by the running plugin; when it does not run, derived from the
    installed configuration and the PLC state.
    ---
    tags:
      - MQTT
    security:
      - BearerAuth: []
    responses:
      200:
        description: status document (state, current error, reconnect, statistics)
      401:
        description: Authentication required
    """
    return jsonify(mqtt_config.status_document(plc_running)), 200


@mqtt_bp.route("/credentials", methods=["GET"])
@jwt_required()
def mqtt_credentials_status() -> tuple[Response, int]:
    """Presence and non-secret metadata of the active and staged credentials.
    ---
    tags:
      - MQTT
    security:
      - BearerAuth: []
    responses:
      200:
        description: "{active: credentialStatus | null, staged: [stagingStatus]}"
      401:
        description: Authentication required
    """
    try:
        doc = mqtt_config.vault().snapshot()
    except (VaultCorrupt, OSError):
        return _internal()
    body = {
        "active": None if doc.active is None else doc.active.to_api(),
        "staged": [item.to_api() for item in doc.staged],
    }
    return jsonify(body), 200


@mqtt_bp.route("/credentials/staging", methods=["POST"])
@jwt_required()
def mqtt_stage_credential() -> tuple[Response, int]:
    """Stage a credential for the next upload (multipart, write-only).

    Part ``request`` (JSON stagingRequest) plus ``password``, or
    ``client_certificate`` and ``client_key`` (optional
    ``client_key_passphrase``). Validated before it is accepted; sealed at
    rest; expires after the staging TTL; usable once, by its creator.
    ---
    tags:
      - MQTT
    security:
      - BearerAuth: []
    consumes:
      - multipart/form-data
    responses:
      201:
        description: stagingStatus with the credentialRef to put in conf/mqtt.json
      400:
        description: Invalid request or credential material
      401:
        description: Authentication required
      413:
        description: Request or part too large
      429:
        description: Too many staged credentials
    """
    actor = request_actor()
    length = request.content_length
    if length is None:
        return _error(CredentialError("credential_invalid_format", "Content-Length is required"))
    if length > MAX_REQUEST_BYTES:
        mqtt_config.audit("stage", "credential_too_large", actor)
        return _error(
            CredentialError("credential_too_large", f"request exceeds {MAX_REQUEST_BYTES} bytes")
        )
    if actor is None:
        return _error(CredentialError("forbidden", "an authenticated user is required"))
    try:
        staging, payload, certificate = _read_staging()
        staged = mqtt_config.vault().stage(staging, payload, certificate, actor)
    except CredentialError as exc:
        mqtt_config.audit("stage", exc.code, actor)
        return _error(exc)
    except (CryptoUnavailable, VaultCorrupt, OSError):
        mqtt_config.audit("stage", "internal_error", actor)
        return _internal()
    mqtt_config.audit(
        "stage",
        "accepted",
        actor,
        staging_id=staged.staging_id,
        credential=f"{staged.record.credential_id}#{staged.record.revision}",
        method=staged.record.method,
    )
    return jsonify(staged.to_api()), 201


@mqtt_bp.route("/credentials/staging/<staging_id>", methods=["DELETE"])
@jwt_required()
def mqtt_cancel_staging(staging_id: str) -> tuple[Response, int]:
    """Cancel a staged credential; only its creator may.
    ---
    tags:
      - MQTT
    security:
      - BearerAuth: []
    responses:
      204:
        description: Cancelled and destroyed
      403:
        description: Staged by another user
      404:
        description: No such staged credential
      409:
        description: Already used
      410:
        description: Expired
    """
    actor = request_actor()
    if actor is None:
        return _error(CredentialError("forbidden", "an authenticated user is required"))
    if not is_uuid_v4(staging_id):
        return _error(CredentialError("staging_not_found", "no staged credential has this id"))
    try:
        mqtt_config.vault().cancel(staging_id, actor)
    except CredentialError as exc:
        mqtt_config.audit("cancel", exc.code, actor, staging_id=staging_id)
        return _error(exc)
    except (VaultCorrupt, OSError):
        return _internal()
    mqtt_config.audit("cancel", "destroyed", actor, staging_id=staging_id)
    return Response(status=204), 204


@mqtt_bp.route("/credentials/active", methods=["DELETE"])
@jwt_required()
def mqtt_remove_active() -> tuple[Response, int]:
    """Remove the active credential and stop a running session that used it.
    ---
    tags:
      - MQTT
    security:
      - BearerAuth: []
    responses:
      204:
        description: Removed and destroyed
      404:
        description: No active credential
    """
    actor = request_actor()
    if actor is None:
        return _error(CredentialError("forbidden", "an authenticated user is required"))
    try:
        removed = mqtt_config.vault().remove_active()
    except CredentialError as exc:
        mqtt_config.audit("remove", exc.code, actor)
        return _error(exc)
    except (VaultCorrupt, OSError):
        return _internal()
    session = mqtt_config.notify_credential_removed()
    mqtt_config.audit(
        "remove",
        "destroyed",
        actor,
        credential=f"{removed.credential_id}#{removed.revision}",
        session=session,
    )
    return Response(status=204), 204
