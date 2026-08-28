import os
import platform
import re
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

from webserver.logger import get_logger

logger, buffer = get_logger("logger", use_buffer=True)


def is_running_in_container():
    """
    Detect if running inside a container (Docker, Podman, etc.).
    Returns True if running in a container, False otherwise.
    """
    # Check for /.dockerenv file (Docker-specific)
    if os.path.exists("/.dockerenv"):
        return True

    # Check for container environment variables
    if os.environ.get("container") or os.environ.get("DOCKER_CONTAINER"):
        return True

    # Check cgroup for container indicators
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as f:
            cgroup_content = f.read()
            if "docker" in cgroup_content or "kubepods" in cgroup_content:
                return True
            # Also check for containerd/cri-o patterns
            if "/lxc/" in cgroup_content or "containerd" in cgroup_content:
                return True
    except (FileNotFoundError, PermissionError):
        pass

    # Check for container runtime in /proc/1/environ
    try:
        with open("/proc/1/environ", "rb") as f:
            environ_content = f.read().decode("utf-8", errors="ignore")
            if "container=" in environ_content:
                return True
    except (FileNotFoundError, PermissionError):
        pass

    return False


def get_runtime_dir():
    """
    Get the runtime directory path based on the platform.
    On MSYS2/Cygwin, use /run/runtime (which maps to a Windows path).
    On Linux in containers, use /var/run/runtime for Docker volume compatibility.
    On native Linux, use /var/run/runtime for ephemeral data (sockets).

    ``OPENPLC_RUNTIME_DIR`` overrides the platform default (used by tests and
    non-standard deployments so the path is not hard-coded to a read-only root).
    """
    override = os.environ.get("OPENPLC_RUNTIME_DIR")
    if override:
        runtime_dir = Path(override)
    elif platform.system() != "Linux":
        runtime_dir = Path("/run/runtime")
    else:
        runtime_dir = Path("/var/run/runtime")

    # Ensure the directory exists
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def get_persistent_data_dir():
    """
    Get the directory for persistent data (database, .env file).
    On containers (Docker), use /var/run/runtime (mounted as persistent volume).
    On native Linux, use /var/lib/openplc-runtime (survives reboot).
    On MSYS2/Windows, use /run/runtime.

    ``OPENPLC_PERSISTENT_DATA_DIR`` overrides the platform default (used by tests
    and non-standard deployments so the path is not hard-coded to a read-only root).
    """
    override = os.environ.get("OPENPLC_PERSISTENT_DATA_DIR")
    if override:
        data_dir = Path(override)
    elif platform.system() != "Linux":
        # MSYS2/Windows: use /run/runtime
        data_dir = Path("/run/runtime")
    elif is_running_in_container():
        # Container: use /var/run/runtime (expected to be a mounted volume)
        data_dir = Path("/var/run/runtime")
    else:
        # Native Linux: use persistent directory that survives reboot
        data_dir = Path("/var/lib/openplc-runtime")

    # Ensure the directory exists with appropriate permissions
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


RUNTIME_DIR = get_runtime_dir()
PERSISTENT_DATA_DIR = get_persistent_data_dir()
ENV_PATH = PERSISTENT_DATA_DIR / ".env"
DB_PATH = PERSISTENT_DATA_DIR / "restapi.db"
# VPP plugin configs + license blobs live here, OUTSIDE $OPENPLC_DIR/build, so a
# runtime version update (install.sh does ``rm -rf $OPENPLC_DIR/build``) can never
# delete a purchased license. The closed .so still reads them because the runtime
# writes this absolute path into vpp_plugins.conf's config_path field (see
# webserver/plcapp_management.py::apply_vpp_plugin_conf); the C loader passes
# config_path to the plugin verbatim, so only the .so binary itself must stay
# under build/vpp.
#
# Created on demand by apply_vpp_plugin_conf / _write_license_atomically, NOT at
# import: a bare module-scope mkdir is import-time filesystem work that turns a
# permission failure into a hard import crash (the very thing tests/pytest/
# conftest.py exists to work around).
VPP_DATA_DIR = PERSISTENT_DATA_DIR / "vpp"
BASE_DIR = os.path.abspath(os.path.dirname(__file__))


# Function to validate environment variable values
def is_valid_env(var_name, value):
    if var_name == "SQLALCHEMY_DATABASE_URI":
        return value.startswith("sqlite:///")
    elif var_name in ("JWT_SECRET_KEY", "PEPPER"):
        return bool(re.fullmatch(r"[a-fA-F0-9]{64}", value))
    return False


# Function to generate a new .env file with valid defaults
def generate_env_file():
    jwt = secrets.token_hex(32)
    pepper = secrets.token_hex(32)
    uri = f"sqlite:///{DB_PATH}"

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("FLASK_ENV=development\n")
        f.write(f"SQLALCHEMY_DATABASE_URI={uri}\n")
        f.write(f"JWT_SECRET_KEY={jwt}\n")
        f.write(f"PEPPER={pepper}\n")

    os.chmod(ENV_PATH, 0o600)
    logger.info(".env file created at %s", ENV_PATH)

    # Ensure the database file exists and is writable
    # Deletion is required because new secrets will change the database saved hashes
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        logger.warning("Deleted existing database file: %s", DB_PATH)


# Load .env file
if not os.path.isfile(ENV_PATH):
    logger.info(".env file not found, creating one...")
    generate_env_file()

load_dotenv(dotenv_path=ENV_PATH, override=False)

# Mandatory settings – raise immediately if not provided
try:
    for var in ("SQLALCHEMY_DATABASE_URI", "JWT_SECRET_KEY", "PEPPER"):
        val = os.getenv(var)
        if not val or not is_valid_env(var, val):
            raise RuntimeError(f"Environment variable '{var}' is invalid or missing")
except RuntimeError as e:
    logger.error("%s", e)
    # Need to regenerate .env file and remove the database as well
    response = (
        input("Do you want to regenerate the .env file? This will delete your database. [y/N]: ")
        .strip()
        .lower()
    )
    if response == "y":
        logger.info("Regenerating .env with new valid values...")
        generate_env_file()
        load_dotenv(ENV_PATH)
    else:
        logger.error("Exiting due to invalid environment configuration.")
        sys.exit(1)


class Config:  # pylint: disable=too-few-public-methods
    SQLALCHEMY_DATABASE_URI = os.environ["SQLALCHEMY_DATABASE_URI"]
    JWT_SECRET_KEY = os.environ["JWT_SECRET_KEY"]
    PEPPER = os.environ["PEPPER"]


class DevConfig(Config):  # pylint: disable=too-few-public-methods
    SQLALCHEMY_TRACK_MODIFICATIONS = False  # keep performance parity with prod
    DEBUG = True


class ProdConfig(Config):  # pylint: disable=too-few-public-methods
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    DEBUG = False
    ENV = "production"
