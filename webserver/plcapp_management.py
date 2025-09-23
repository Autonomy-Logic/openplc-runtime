from dataclasses import dataclass, field
from enum import Enum, auto
import logging
import os
import zipfile
import subprocess
import threading
from typing import Final

logger = logging.getLogger(__name__)

MAX_FILE_SIZE: Final[int] = 10 * 1024 * 1024   # 10 MB per file
MAX_TOTAL_SIZE: Final[int] = 50 * 1024 * 1024  # 50 MB total
DISALLOWED_EXT = {".exe", ".dll", ".sh", ".bat", ".js", ".vbs", ".scr"}

class BuildStatus(Enum):
    IDLE = auto()
    UNZIPPING = auto()
    COMPILING = auto()
    SUCCESS = auto()
    FAILED = auto()

@dataclass
class BuildProcess:
    status: BuildStatus = BuildStatus.IDLE
    logs: list[str] = field(default_factory=list)
    exit_code: int | None = None

    def log(self, msg: str):
        logger.info(msg)
        self.logs.append(msg)


build_state = BuildProcess()  # global-ish singleton for status


def analyze_zip(zip_path) -> (bool, list):
    """Analyze the ZIP file for safety before extraction."""
    build_state.status = BuildStatus.UNZIPPING

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
                logger.warning("Dangerous path: %s", filename)
                safe = False

            # Check uncompressed size
            if uncompressed_size > MAX_FILE_SIZE:
                logger.warning("File too large: %s (%d bytes)",
                               filename, uncompressed_size)
                safe = False

            # Check compression ratio (ZIP bomb detection)
            if compressed_size > 0 and uncompressed_size / compressed_size > 1000:
                logger.warning("Suspicious compression ratio in %s",
                               filename)
                safe = False

            # Check disallowed extensions
            if ext in DISALLOWED_EXT:
                logger.warning("Disallowed extension: %s",
                               filename)
                safe = False

            total_size += uncompressed_size
            valid_files.append(info)

        # Check total size
        if total_size > MAX_TOTAL_SIZE:
            logger.warning("Total uncompressed size too large: %d bytes", 
                           total_size)
            safe = False

        if safe:
            logger.info("ZIP file looks safe to extract (based on static checks).")
        else:
            logger.warning("ZIP file failed safety checks.")

        return safe, valid_files


def safe_extract(zip_path, dest_dir, valid_files):
    """Extract files safely to a target directory."""
    build_state.status = BuildStatus.UNZIPPING
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
                logger.warning("Skipping suspicious path: %s", filename)
                continue

            # Create parent directories if needed
            os.makedirs(os.path.dirname(out_path), exist_ok=True)

            # Extract file
            with zf.open(info) as src, open(out_path, "wb") as dst:
                dst.write(src.read())

            logger.info("Extracted: %s", out_path)

def run_compile(cwd: str = "core/generated"):
    """Run compile script asynchronously and update status/logs."""
    script_path: str = "./scripts/compile.sh"

    build_state.status = BuildStatus.COMPILING
    build_state.log(f"[INFO] Starting compilation: {script_path}\n")

    def stream_output(pipe, prefix):
        for line in iter(pipe.readline, ''):
            msg = f"{prefix}{line}"
            build_state.log(msg)
        pipe.close()

    def wait_and_finish():
        exit_code = process.wait()
        build_state.exit_code = exit_code
        if exit_code == 0:
            build_state.status = BuildStatus.SUCCESS
            build_state.log("[INFO] Compilation succeeded\n")
        else:
            build_state.status = BuildStatus.FAILED
            build_state.log(f"[INFO] Compilation failed (exit={exit_code})\n")
            return {"CompilationStatusFail": f"Compilation failed (exit={exit_code})"}
    
    process = subprocess.Popen(
        ["bash", script_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    threading.Thread(target=stream_output, args=(process.stdout, "[OUT] "), daemon=True).start()
    threading.Thread(target=stream_output, args=(process.stderr, "[ERR] "), daemon=True).start()

    task_wait = threading.Thread(target=wait_and_finish, daemon=True)
    task_wait.start()
    task_wait.join(timeout=0.1)
    
    process = subprocess.Popen(
        ["bash", "./scripts/compile-clean.sh"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    task_wait = threading.Thread(target=wait_and_finish, daemon=True)
    task_wait.start()
    task_wait.join(timeout=0.1)
