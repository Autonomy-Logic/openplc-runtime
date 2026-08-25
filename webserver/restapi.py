import os
from typing import Callable, Optional

from flask import Blueprint, Flask, jsonify, request
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    current_user,
    get_jwt,
    jwt_required,
    verify_jwt_in_request,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text
from werkzeug.security import check_password_hash, generate_password_hash

import webserver.config
from webserver.logger import get_logger
from webserver.retain_config import (
    DEFAULT_FLUSH_SECONDS,
    DEFAULT_RETAIN_PATH,
    MAX_FLUSH_SECONDS,
    MIN_FLUSH_SECONDS,
    RetainConfigError,
    read_retain_config,
    write_retain_config,
)
from webserver.version import MIN_EDITOR_VERSION, RUNTIME_VERSION

logger, buffer = get_logger("logger", use_buffer=True)

env = os.getenv("FLASK_ENV", "development")

app_restapi = Flask(__name__)

if env == "production":
    app_restapi.config.from_object(webserver.config.ProdConfig)
else:
    app_restapi.config.from_object(webserver.config.DevConfig)

restapi_bp = Blueprint("restapi_blueprint", __name__)
_handler_callback_get: Optional[Callable[[str, dict], dict]] = None
_handler_callback_post: Optional[Callable[[str, dict], dict]] = None


@restapi_bp.after_request
def add_runtime_version_header(response):
    """Add runtime version header to all API responses for version detection.

    Editors gate firmware uploads on this value — the v4.1.x runtime
    ships the STruC++ compile pipeline, which is not wire-compatible
    with the MatIEC pipeline 4.0.x runtimes shipped.
    """
    response.headers["X-OpenPLC-Runtime-Version"] = RUNTIME_VERSION
    return response


@restapi_bp.route("/version", methods=["GET"])
def restapi_version():
    """Return the runtime version.

    Unauthenticated on purpose — editors call this before login to
    decide whether their compile pipeline is compatible.  The string
    is the GitHub release tag baked in at build time (e.g.
    ``v4.1.0-rc.3``); ``dev`` means the image was built from source
    without a CI tag.
    ---
    tags:
      - Runtime
    responses:
      200:
        description: Runtime version retrieved
        schema:
          type: object
          properties:
            version:
              type: string
              description: Runtime version string (GitHub release tag)
    """
    return jsonify({"version": RUNTIME_VERSION}), 200


@restapi_bp.route("/capabilities", methods=["GET"])
def restapi_capabilities():
    """Return what this runtime is and what it requires of an editor.

    Unauthenticated, like ``/version`` — the editor calls this before
    login to decide whether it may talk to this device at all.

    ``minEditorVersion`` is the oldest editor this runtime accepts
    programs from.  The runtime only ADVERTISES it: the editor compares
    the value against its own version and refuses to upload.  Nothing on
    the upload path enforces it, so shipping a new runtime can never lock
    out an editor already installed in the field (DOPE-448).

    Editors that predate this endpoint get a 404 and fall back to
    ``/version``; they simply see no editor floor, which is exactly the
    behaviour they had before.
    ---
    tags:
      - Runtime
    responses:
      200:
        description: Runtime capabilities retrieved
        schema:
          type: object
          properties:
            runtimeVersion:
              type: string
              description: Runtime version string (GitHub release tag)
            minEditorVersion:
              type: string
              description: Oldest OpenPLC Editor version this runtime accepts programs from
    """
    return (
        jsonify(
            {
                "runtimeVersion": RUNTIME_VERSION,
                "minEditorVersion": MIN_EDITOR_VERSION,
            }
        ),
        200,
    )


jwt = JWTManager(app_restapi)
db = SQLAlchemy(app_restapi)

jwt_blacklist = set()

# Role-based access control.  For now there are exactly two roles: ``admin``
# (may manage every account) and ``user`` (may edit only its own account and
# cannot create or delete accounts).  Enforcement lives server-side in the
# endpoints below — the editor UI mirrors it but is never the boundary.
ADMIN_ROLE = "admin"
USER_ROLE = "user"
ROLES = (ADMIN_ROLE, USER_ROLE)


@jwt.token_in_blocklist_loader
def check_if_token_revoked(jwt_header, jwt_payload):
    try:
        jti = jwt_payload["jti"]
        return jti in jwt_blacklist
    except KeyError as e:
        logger.error("Error revoking JWT: %s", e)
    except Exception as e:
        logger.error("Error revoking JWT: %s", e)
    return False


class User(db.Model):  # type: ignore[name-defined]
    __tablename__ = "users"

    id: int = db.Column(db.Integer, primary_key=True)
    username: str = db.Column(db.Text, nullable=False, unique=True)
    password_hash: str = db.Column(db.Text, nullable=False)
    # RBAC role. Defaults to ``admin`` so that databases predating this column
    # (migrated in place) and any insert that omits the role resolve to the
    # privileged role — the pre-RBAC runtime treated every user as an admin.
    role: str = db.Column(db.Text, nullable=False, default=ADMIN_ROLE, server_default=ADMIN_ROLE)

    # Use PBKDF2 with SHA256 and 600,000 iterations for password hashing
    derivation_method: str = "pbkdf2:sha256:600000"

    def set_password(self, password: str) -> str:
        password = password + app_restapi.config["PEPPER"]
        self.password_hash = generate_password_hash(password, method=self.derivation_method)
        return self.password_hash

    def check_password(self, password: str) -> bool:
        password = password + app_restapi.config["PEPPER"]
        return check_password_hash(self.password_hash, password)

    def is_admin(self) -> bool:
        return self.role == ADMIN_ROLE

    def to_dict(self):
        return {"id": self.id, "username": self.username, "role": self.role}


def admin_count() -> int:
    """Number of accounts holding the admin role (used by last-admin guards)."""
    return User.query.filter_by(role=ADMIN_ROLE).count()


def apply_user_schema_migrations():
    """Add the ``role`` column to a pre-RBAC ``users`` table in place.

    ``db.create_all()`` only creates missing tables — it never alters an
    existing one — so a runtime upgraded from a build without RBAC keeps its
    old schema.  Add the column idempotently and backfill existing rows to
    ``admin`` (they were the sole managing account before roles existed).
    Safe to call on every boot; a no-op once the column exists.
    """
    inspector = sa_inspect(db.engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("users")}
    if "role" in columns:
        return
    with db.engine.begin() as conn:
        conn.execute(
            sa_text(f"ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT '{ADMIN_ROLE}'")
        )


@jwt.user_identity_loader
def user_identity_lookup(user):
    return str(user.id)


@jwt.user_lookup_loader
def user_lookup_callback(_jwt_header, jwt_data):
    identity = jwt_data["sub"]
    return User.query.filter_by(id=identity).one_or_none()


def register_callback_get(callback: Callable[[str, dict], dict]):
    global _handler_callback_get
    _handler_callback_get = callback
    logger.debug("GET Callback registered successfully for rest_blueprint!")


def register_callback_post(callback: Callable[[str, dict], dict]):
    global _handler_callback_post
    _handler_callback_post = callback
    logger.debug("POST Callback registered successfully for rest_blueprint!")


@restapi_bp.route("/create-user", methods=["POST"])
def create_user():
    """
    Create a new user account.
    ---
    tags:
      - Authentication
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - username
            - password
          properties:
            username:
              type: string
              example: admin
            password:
              type: string
              example: openplc
    responses:
      201:
        description: User created successfully
        schema:
          type: object
          properties:
            msg:
              type: string
              example: User created
            id:
              type: integer
              example: 1
      400:
        description: Missing username or password
      401:
        description: User already created or authentication required
      409:
        description: Username already exists
    """
    # check if there are any users in the database
    try:
        users_exist = User.query.first() is not None
    except Exception as e:
        logger.error("Error checking for users: %s", e)
        return jsonify({"msg": f"User creation error: {e}"}), 401

    # Bootstrap: with no users yet, anyone may create the FIRST account and it
    # is always an admin (someone has to be able to manage accounts). Once any
    # user exists, only an authenticated admin may create further accounts.
    if not users_exist:
        role = ADMIN_ROLE
    else:
        if verify_jwt_in_request(optional=True) is None:
            return jsonify({"msg": "Authentication required"}), 401
        if not (current_user and current_user.is_admin()):
            return jsonify({"msg": "Admin privileges required"}), 403
        role = (request.get_json() or {}).get("role", USER_ROLE)
        if role not in ROLES:
            return jsonify({"msg": f"Invalid role. Must be one of: {', '.join(ROLES)}"}), 400

    data = request.get_json()
    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify({"msg": "Missing username or password"}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({"msg": "Username already exists"}), 409

    # Create a new user
    user = User(username=username, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    return jsonify({"msg": "User created", "id": user.id, "role": user.role}), 201


# verify existing users individually
@restapi_bp.route("/get-user-info/<int:user_id>", methods=["GET"])
@jwt_required()
def get_user_info(user_id):
    """
    Get information about a specific user.
    ---
    tags:
      - Users
    security:
      - BearerAuth: []
    parameters:
      - name: user_id
        in: path
        required: true
        type: integer
        description: The user ID
    responses:
      200:
        description: User information retrieved successfully
        schema:
          type: object
          properties:
            id:
              type: integer
              example: 1
            username:
              type: string
              example: admin
      404:
        description: User not found
      500:
        description: User retrieval error
    """
    try:
        user = User.query.get(user_id)
    except Exception as e:
        logger.error("Error retrieving user: %s", e)
        return jsonify({"msg": f"User retrieval error: {e}"}), 500

    if not user:
        return jsonify({"msg": "User not found"}), 404

    return jsonify(user.to_dict())


@restapi_bp.route("/get-users-info", methods=["GET"])
def get_users_info():
    """
    Get information about all users or check if users exist.
    ---
    tags:
      - Users
    security:
      - BearerAuth: []
    responses:
      200:
        description: Users information retrieved successfully
        schema:
          type: array
          items:
            type: object
            properties:
              id:
                type: integer
              username:
                type: string
      404:
        description: No users found
      500:
        description: User retrieval error
    """
    # If there are no users, we don't need to verify JWT
    try:
        verify_jwt_in_request()
    except Exception:
        # logger.warning(
        #     "No JWT token provided, checking for users without authentication"
        # )
        try:
            users_exist = User.query.first() is not None
        except Exception:
            # logger.error("Error checking for users: %s", e)
            return jsonify({"msg": "User retrieval error"}), 500

        if not users_exist:
            return jsonify({"msg": "No users found"}), 404
        return jsonify({"msg": "Users found"}), 200

    try:
        users = User.query.all()
    except Exception as e:
        logger.error("Error retrieving users: %s", e)
        return jsonify({"msg": "User retrieval error"}), 500

    return jsonify([user.to_dict() for user in users]), 200


@restapi_bp.route("/whoami", methods=["GET"])
@jwt_required()
def whoami():
    """
    Return the currently authenticated user (id, username, role).
    ---
    tags:
      - Users
    security:
      - BearerAuth: []
    responses:
      200:
        description: Current user information
        schema:
          type: object
          properties:
            id:
              type: integer
            username:
              type: string
            role:
              type: string
      401:
        description: Authentication required
    """
    return jsonify(current_user.to_dict()), 200


# Unified user update: rename, change password and/or change role in one call.
# Only the fields present in the body are applied.  Authorization:
#   - admin may update ANY user (username, password, role);
#   - a non-admin may update ONLY its own account and may never change its role;
#   - changing YOUR OWN password requires the current password (blocks a stolen
#     token / unlocked session from silently resetting the password); an admin
#     resetting ANOTHER user's password does not need it.
@restapi_bp.route("/update-user/<int:user_id>", methods=["PUT"])
@jwt_required()
def update_user(user_id):
    """
    Update a user's username, password and/or role.
    ---
    tags:
      - Users
    security:
      - BearerAuth: []
    parameters:
      - name: user_id
        in: path
        required: true
        type: integer
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            username:
              type: string
            password:
              type: string
            current_password:
              type: string
              description: Required when changing your own password
            role:
              type: string
              enum: [admin, user]
    responses:
      200:
        description: User updated
      400:
        description: Invalid or missing fields
      403:
        description: Not permitted / current password incorrect
      404:
        description: User not found
      409:
        description: Username taken or last-admin protection
    """
    data = request.get_json() or {}
    target = User.query.get(user_id)
    if not target:
        return jsonify({"msg": "User not found"}), 404

    caller = current_user
    caller_is_admin = bool(caller and caller.is_admin())
    is_self = bool(caller and caller.id == target.id)

    if not caller_is_admin and not is_self:
        return jsonify({"msg": "You can only edit your own account"}), 403

    new_username = data.get("username")
    new_password = data.get("password")
    new_role = data.get("role")

    if new_username is None and new_password is None and new_role is None:
        return jsonify({"msg": "No fields to update"}), 400

    # Role change — admin only, with last-admin protection.
    if new_role is not None:
        if not caller_is_admin:
            return jsonify({"msg": "Only an admin can change roles"}), 403
        if new_role not in ROLES:
            return jsonify({"msg": f"Invalid role. Must be one of: {', '.join(ROLES)}"}), 400
        if target.is_admin() and new_role != ADMIN_ROLE and admin_count() <= 1:
            return jsonify({"msg": "Cannot demote the last remaining admin"}), 409
        target.role = new_role

    # Username change — must be non-empty and unique.
    if new_username is not None:
        new_username = new_username.strip()
        if not new_username:
            return jsonify({"msg": "Username cannot be empty"}), 400
        clash = User.query.filter_by(username=new_username).first()
        if clash and clash.id != target.id:
            return jsonify({"msg": "Username already exists"}), 409
        target.username = new_username

    # Password change — self-change requires the current password.
    self_password_changed = False
    if new_password is not None:
        if not new_password:
            return jsonify({"msg": "Password cannot be empty"}), 400
        if is_self:
            current_password = data.get("current_password")
            if not current_password:
                return jsonify({"msg": "Current password is required"}), 400
            if not target.check_password(current_password):
                return jsonify({"msg": "Current password is incorrect"}), 403
            self_password_changed = True
        target.set_password(new_password)

    db.session.commit()

    # Changing your own password invalidates your current session: revoke the
    # token used for this request so a stolen/old token can't outlive the
    # change. The client must re-authenticate with the new password.
    if self_password_changed:
        revoke_jwt()

    return jsonify({"msg": "User updated successfully", "user": target.to_dict()}), 200


# password change for specific user by any authenticated user
@restapi_bp.route("/password-change/<int:user_id>", methods=["PUT"])
@jwt_required()
def change_password(user_id):
    """
    Change password for a specific user.
    ---
    tags:
      - Users
    security:
      - BearerAuth: []
    parameters:
      - name: user_id
        in: path
        required: true
        type: integer
        description: The user ID
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - old_password
            - new_password
          properties:
            old_password:
              type: string
            new_password:
              type: string
    responses:
      200:
        description: Password updated successfully
        schema:
          type: object
          properties:
            msg:
              type: string
              example: Password for user admin updated successfully
      400:
        description: Missing old or new password
      403:
        description: Old password is incorrect
      404:
        description: User not found
      500:
        description: User retrieval error
    """
    data = request.get_json()
    old_password = data.get("old_password")
    new_password = data.get("new_password")

    if not old_password or not new_password:
        return jsonify({"msg": "Both old and new passwords are required"}), 400

    try:
        user = User.query.get(user_id)
    except Exception as e:
        logger.error("Error retrieving user: %s", e)
        return jsonify({"msg": "User retrieval error"}), 500

    if not user:
        return jsonify({"msg": "User not found"}), 404

    if not user.check_password(old_password):
        return jsonify({"msg": "Old password is incorrect"}), 403

    user.set_password(new_password)
    db.session.commit()

    return (
        jsonify({"msg": f"Password for user {user.username} updated successfully"}),
        200,
    )


# delete a user by ID
@restapi_bp.route("/delete-user/<int:user_id>", methods=["DELETE"])
@jwt_required()
def delete_user(user_id):
    """
    Delete a user by ID.
    ---
    tags:
      - Users
    security:
      - BearerAuth: []
    parameters:
      - name: user_id
        in: path
        required: true
        type: integer
        description: The user ID to delete
    responses:
      200:
        description: User deleted successfully
        schema:
          type: object
          properties:
            msg:
              type: string
              example: User admin deleted successfully
      403:
        description: Admin privileges required or cannot delete own account
      404:
        description: User not found
      500:
        description: User retrieval error
    """
    try:
        user = User.query.get(user_id)
    except Exception as e:
        logger.error("Error retrieving user: %s", e)
        return jsonify({"msg": f"User retrieval error: {e}"}), 500

    if not user:
        return jsonify({"msg": "User not found"}), 404

    # Only admins may delete accounts.
    if not (current_user and current_user.is_admin()):
        return jsonify({"msg": "Admin privileges required"}), 403

    # You cannot delete your own account. This also guarantees the "never remove
    # all users" invariant: only an admin can delete, an admin can't delete
    # itself, so the acting admin always survives the operation.
    if current_user.id == user.id:
        return jsonify({"msg": "You cannot delete your own account"}), 403

    db.session.delete(user)
    db.session.commit()
    return jsonify({"msg": f"User {user.username} deleted successfully"}), 200


# login endpoint
# Persistent storage (RETAIN) settings for the runtime's BUILT-IN file store.
#
# Read by any authenticated user, written by an admin — the same split the user
# endpoints use, and for the same reason: seeing how a device is configured is
# not the same privilege as changing it.
#
# The settings describe the built-in store only. A VPP plugin that provides its
# own retain backend OVERRIDES it, which is why the GET also reports what is
# actually holding the bytes right now.
@restapi_bp.route("/retain-config", methods=["GET"])
@jwt_required()
def get_retain_config():
    """
    Return the persistent-storage settings and the live backend.
    ---
    tags:
      - Runtime
    security:
      - BearerAuth: []
    responses:
      200:
        description: Persistent storage settings
        schema:
          type: object
          properties:
            enabled:
              type: boolean
            path:
              type: string
            flushSeconds:
              type: integer
            defaultPath:
              type: string
            minFlushSeconds:
              type: integer
            maxFlushSeconds:
              type: integer
            backend:
              type: string
              description: What is holding retained bytes now — none, plugin, file or unknown
            backendDetail:
              type: string
            active:
              type: boolean
      401:
        description: Authentication required
    """
    cfg = read_retain_config()
    manager = app_restapi.config.get("RUNTIME_MANAGER")
    status = manager.retain_status() if manager else {"active": False, "backend": "unknown", "detail": ""}
    cfg.update(
        {
            "defaultPath": DEFAULT_RETAIN_PATH,
            "defaultFlushSeconds": DEFAULT_FLUSH_SECONDS,
            "minFlushSeconds": MIN_FLUSH_SECONDS,
            "maxFlushSeconds": MAX_FLUSH_SECONDS,
            "backend": status.get("backend", "unknown"),
            "backendDetail": status.get("detail", ""),
            "active": status.get("active", False),
        }
    )
    return jsonify(cfg), 200


@restapi_bp.route("/retain-config", methods=["PUT"])
@jwt_required()
def put_retain_config():
    """
    Update the persistent-storage settings.

    Takes effect when the PLC next starts: the core reads the file once per
    program load, and re-reading it mid-scan would mean changing where a
    running program's retained values go, which is not a thing to do quietly.
    ---
    tags:
      - Runtime
    security:
      - BearerAuth: []
    parameters:
      - in: body
        name: body
        schema:
          type: object
          properties:
            enabled:
              type: boolean
            path:
              type: string
            flushSeconds:
              type: integer
    responses:
      200:
        description: Settings saved
      400:
        description: A setting the runtime could not honour
      401:
        description: Authentication required
      403:
        description: Admin privileges required
    """
    if not (current_user and current_user.is_admin()):
        return jsonify({"msg": "Admin privileges required"}), 403

    data = request.get_json() or {}
    current = read_retain_config()

    enabled = data.get("enabled", current["enabled"])
    if not isinstance(enabled, bool):
        return jsonify({"msg": "enabled must be true or false"}), 400

    path = data.get("path", current["path"])
    flush_seconds = data.get("flushSeconds", current["flushSeconds"])

    try:
        saved = write_retain_config(enabled, path, flush_seconds)
    except RetainConfigError as e:
        return jsonify({"msg": str(e)}), 400
    except OSError as e:
        return jsonify({"msg": f"Could not save the settings: {e}"}), 400

    return jsonify(saved), 200


@restapi_bp.route("/login", methods=["POST"])
def login():
    """
    Authenticate user and get JWT token.
    ---
    tags:
      - Authentication
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - username
            - password
          properties:
            username:
              type: string
              example: admin
            password:
              type: string
              example: openplc
    responses:
      200:
        description: Login successful
        schema:
          type: object
          properties:
            access_token:
              type: string
              description: JWT access token
      401:
        description: Invalid credentials
      500:
        description: User retrieval error
    """
    username = request.json.get("username", None)
    password = request.json.get("password", None)

    try:
        user = User.query.filter_by(username=username).one_or_none()
        logger.debug("User found: %s", user)
    except Exception as e:
        logger.error("Error retrieving user: %s", e)
        return jsonify({"msg": f"User retrieval error: {e}"}), 500

    if not user or not user.check_password(password):
        return jsonify("Wrong username or password"), 401

    access_token = create_access_token(identity=user)
    return jsonify(access_token=access_token)


# logout endpoint
@restapi_bp.route("/logout", methods=["POST"])
@jwt_required()
def logout():
    """
    Logout user and revoke JWT token.
    ---
    tags:
      - Authentication
    security:
      - BearerAuth: []
    responses:
      200:
        description: Logout successful
        schema:
          type: object
          properties:
            msg:
              type: string
              example: User logged out successfully
    """
    revoke_jwt()
    return jsonify({"msg": "User logged out successfully"}), 200


def revoke_jwt():
    # Add the JWT ID to the blacklist
    try:
        jti = get_jwt()["jti"]
        jwt_blacklist.add(jti)
    except KeyError as e:
        logger.error("Error revoking JWT: %s", e)
    except AttributeError as e:
        logger.error("Error revoking JWT: %s", e)
    except Exception as e:
        logger.error("Error revoking JWT: %s", e)


@restapi_bp.route("/<command>", methods=["GET"])
@jwt_required()
def restapi_plc_get(command):
    """
    Execute PLC control commands (GET).
    ---
    tags:
      - PLC Control
    security:
      - BearerAuth: []
    parameters:
      - name: command
        in: path
        required: true
        type: string
        enum:
          - start-plc
          - stop-plc
          - status
          - ping
          - runtime-logs
          - compilation-status
          - serial-ports
        description: |
          Command to execute:
          - start-plc: Start the PLC runtime
          - stop-plc: Stop the PLC runtime
          - status: Get PLC status (optional query param include_stats=true)
          - ping: Check if runtime is responsive
          - runtime-logs: Get runtime logs (optional query params: id, level)
          - compilation-status: Get current compilation status
          - serial-ports: List available serial ports
      - name: id
        in: query
        required: false
        type: integer
        description: Minimum log ID (for runtime-logs)
      - name: level
        in: query
        required: false
        type: string
        description: Log level filter (for runtime-logs)
      - name: include_stats
        in: query
        required: false
        type: string
        enum:
          - "true"
          - "false"
        description: Include timing stats (for status)
    responses:
      200:
        description: Command executed successfully
        schema:
          type: object
          properties:
            status:
              type: string
              description: Response status or PLC state
      500:
        description: No handler registered or execution error
    """
    if _handler_callback_get is None:
        return jsonify({"error": "No handler registered"}), 500

    try:
        data = request.args.to_dict()
        result = _handler_callback_get(command, data)
        return jsonify(result), 200
    except Exception as e:
        # logger.error("Error in restapi_plc_get: %s", e)
        return jsonify({"error": str(e)}), 500


@restapi_bp.route("/<command>", methods=["POST"])
@jwt_required()
def restapi_plc_post(command):
    """
    Execute PLC control commands (POST).
    ---
    tags:
      - Programs
    security:
      - BearerAuth: []
    consumes:
      - multipart/form-data
    parameters:
      - name: command
        in: path
        required: true
        type: string
        enum:
          - upload-file
        description: |
          Command to execute:
          - upload-file: Upload a PLC program ZIP file for compilation
      - name: file
        in: formData
        required: true
        type: file
        description: ZIP file containing PLC program
    responses:
      200:
        description: Command executed successfully
        schema:
          type: object
          properties:
            UploadFileFail:
              type: string
              description: Error message (empty on success)
            CompilationStatus:
              type: string
              enum:
                - IDLE
                - UNZIPPING
                - COMPILING
                - SUCCESS
                - FAILED
              description: Current compilation status
      500:
        description: No handler registered or execution error
    """
    if _handler_callback_post is None:
        return jsonify({"error": "No handler registered"}), 500

    try:
        data = request.get_json(silent=True) or {}
        result = _handler_callback_post(command, data)
        return jsonify(result), 200
    except Exception as e:
        # logger.error("Error in restapi_plc_post: %s", e)
        return jsonify({"error": str(e)}), 500
