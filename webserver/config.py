import os
import re
import secrets
from pathlib import Path

from dotenv import load_dotenv
from webserver.logger import get_logger, LogParser

logger, buffer = get_logger("logger", use_buffer=True)

# Use /var/run/runtime for persistent data to avoid Docker volume permission issues
# This directory is created in the Dockerfile and can use a named volume
ENV_PATH = Path("/var/run/runtime/.env")
DB_PATH = Path("/var/run/runtime/restapi.db")
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

    with open(ENV_PATH, "w") as f:
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
        input(
            "Do you want to regenerate the .env file? This will delete your database. [y/N]: "
        )
        .strip()
        .lower()
    )
    if response == "y":
        print("Regenerating .env with new valid values...")
        generate_env_file()
        load_dotenv(ENV_PATH)
    else:
        print("Exiting due to invalid environment configuration.")
        exit(1)


class Config:
    SQLALCHEMY_DATABASE_URI = os.environ["SQLALCHEMY_DATABASE_URI"]
    JWT_SECRET_KEY = os.environ["JWT_SECRET_KEY"]
    PEPPER = os.environ["PEPPER"]


class DevConfig(Config):
    SQLALCHEMY_TRACK_MODIFICATIONS = False  # keep performance parity with prod
    DEBUG = True


class ProdConfig(Config):
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    DEBUG = False
    ENV = "production"
