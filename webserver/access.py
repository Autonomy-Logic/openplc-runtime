# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Roles and the access policy of every runtime endpoint.

Four roles. ``admin`` manages accounts and retrieves projects, plus everything
``engineer`` does; ``engineer`` operates the PLC (upload, start/stop, logs,
discovery, plugin commands, the debugger, the user list); ``operator`` and
``viewer`` are HMI accounts that reach only sign-in, sign-out, renewal, whoami,
the read-only PLC status and their own password.

``POLICY`` is the single table that says which roles reach which endpoint; the
``allow`` decorator applies it to a Flask view and records it on the view, so a
test can check that every registered route is covered.
"""

from collections.abc import Callable
from functools import wraps

from flask import jsonify
from flask_jwt_extended import current_user, verify_jwt_in_request

from webserver.logger import get_logger

logger, _ = get_logger("logger", use_buffer=True)

ADMIN_ROLE = "admin"
ENGINEER_ROLE = "engineer"
OPERATOR_ROLE = "operator"
VIEWER_ROLE = "viewer"
ROLES: tuple[str, ...] = (ADMIN_ROLE, ENGINEER_ROLE, OPERATOR_ROLE, VIEWER_ROLE)

# The role every account held before the four roles; stored accounts migrate to engineer.
LEGACY_USER_ROLE = "user"

ADMIN_ONLY: frozenset[str] = frozenset({ADMIN_ROLE})
ENGINEERING: frozenset[str] = frozenset({ADMIN_ROLE, ENGINEER_ROLE})
EVERY_ROLE: frozenset[str] = frozenset(ROLES)

# Commands of GET /api/<command> that HMI accounts may run: the read-only PLC status.
STATUS_COMMANDS: frozenset[str] = frozenset({"status", "ping"})

POLICY: dict[str, frozenset[str]] = {
    "whoami": EVERY_ROLE,
    "logout": EVERY_ROLE,
    "renew": EVERY_ROLE,
    "plc-status": EVERY_ROLE,
    "get-user-info": ENGINEERING,
    "get-users-info": ENGINEERING,
    "update-user": EVERY_ROLE,
    "rename-account": ENGINEERING,
    "create-user": ADMIN_ONLY,
    "delete-user": ADMIN_ONLY,
    "project-snapshot": ADMIN_ONLY,
    "plc-command": ENGINEERING,
    "plc-upload": ENGINEERING,
    "discovery": ENGINEERING,
    "debugger": ENGINEERING,
    "hmi": EVERY_ROLE,
    "hardware-id": EVERY_ROLE,
    "runtime-license": ENGINEERING,
}

# Routes reached without a signed-in account, by endpoint name.
PUBLIC_ENDPOINTS: frozenset[str] = frozenset(
    {
        "static",
        "restapi_blueprint.restapi_version",
        "restapi_blueprint.restapi_capabilities",
        "restapi_blueprint.login",
        # The stored runtime licence and hardware ID, read by the editor before it signs in.
        "restapi_blueprint.get_runtime_licenses",
        # The HMI client (sealed static files); its page shows the sign-in form.
        "hmi_blueprint.hmi_redirect",
        "hmi_blueprint.hmi_index",
        "hmi_blueprint.hmi_asset",
    }
)

# The ``/api/<command>`` dispatcher: checks each command against ``command_key`` inside the view.
COMMAND_ENDPOINTS: frozenset[str] = frozenset(
    {"restapi_blueprint.restapi_plc_get", "restapi_blueprint.restapi_plc_post"}
)

# Routes that authenticate inside the view because they also serve a caller with no account yet.
SELF_AUTHENTICATING_ENDPOINTS: frozenset[str] = frozenset(
    {"restapi_blueprint.create_user", "restapi_blueprint.get_users_info"}
)


def invalid_role_message(role: object) -> str:
    """The refusal for a role a request names that does not exist."""
    if role == LEGACY_USER_ROLE:
        return "The role 'user' is now 'engineer'. Use one of: " + ", ".join(ROLES)
    return "Invalid role. Must be one of: " + ", ".join(ROLES)


def may(role: str | None, key: str) -> bool:
    """Whether an account with ``role`` reaches the endpoint ``key`` of ``POLICY``."""
    return role is not None and role in POLICY[key]


def command_key(method: str, command: str) -> str:
    """The ``POLICY`` key of a command of the ``/api/<command>`` dispatcher."""
    if method == "GET" and command in STATUS_COMMANDS:
        return "plc-status"
    return "plc-upload" if method == "POST" else "plc-command"


def refuse(role: str | None, what: str):
    """A 403 for ``role`` on ``what``, logged as a refused access."""
    logger.warning("Refused %s for role %s", what, role or "none")
    return jsonify({"msg": "This account's role may not use this endpoint"}), 403


View = Callable[..., object]


def allow(key: str) -> Callable[[View], View]:
    """Requires a signed-in account of a role in ``POLICY[key]``; records ``key`` on the view."""
    roles = POLICY[key]

    def decorate(view: View) -> View:
        @wraps(view)
        def guarded(*args: object, **kwargs: object):
            verify_jwt_in_request()
            role = current_user.role if current_user else None
            if role not in roles:
                return refuse(role, key)
            return view(*args, **kwargs)

        guarded.access_key = key
        return guarded

    return decorate
