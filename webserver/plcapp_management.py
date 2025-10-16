from dataclasses import dataclass, field
from enum import Enum, auto
import os
import zipfile
import subprocess
import threading
from typing import Final

from webserver.runtimemanager import RuntimeManager
from webserver.logger import get_logger, LogParser

logger, _ = get_logger("runtime", use_buffer=True)


MAX_FILE_SIZE: Final[int] = 10 * 1024 * 1024   # 10 MB per file
MAX_TOTAL_SIZE: Final[int] = 50 * 1024 * 1024  # 50 MB total
DISALLOWED_EXT = (".exe", ".dll", ".sh", ".bat", ".js", ".vbs", ".scr")

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
        # logger.info(msg)
        self.logs.append(msg)

    def clear(self):
        self.status = BuildStatus.IDLE
        self.logs.clear()
        self.exit_code = None


build_state = BuildProcess()  # global-ish singleton for status


def analyze_zip(zip_path) -> tuple[bool, list]:
    """Analyze the ZIP file for safety before extraction."""
    build_state.status = BuildStatus.UNZIPPING

    if not zipfile.is_zipfile(zip_path):
        build_state.log("[ERROR] Not a valid PLC Program file.\n")
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
                # logger.warning("Dangerous path: %s", filename)
                safe = False

            # Check uncompressed size
            if uncompressed_size > MAX_FILE_SIZE:
                logger.warning("File too large: %s (%d bytes)",
                                filename, uncompressed_size)
                safe = False

            # Check compression ratio (ZIP bomb detection)
            if compressed_size > 0 and uncompressed_size / compressed_size > 1000:
                # logger.warning("Suspicious compression ratio in %s",
                            #    filename)
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
            # logger.warning("Total uncompressed size too large: %d bytes", 
            #                total_size)
            safe = False

        if safe:
            logger.debug("ZIP file looks safe to extract (based on static checks).")
        else:
            logger.warning("ZIP file failed safety checks.")

        return safe, valid_files


def safe_extract(zip_path, dest_dir, valid_files):
    """Extract files safely to a target directory.
    - Skips macOS metadata (__MACOSX, .DS_Store)
    - Auto-strips a single common root folder if present
    """
    build_state.status = BuildStatus.UNZIPPING

    with zipfile.ZipFile(zip_path, "r") as zf:
        # Detect roots (ignoring macOS junk)
        roots = set()
        for info in valid_files:
            if info.filename.startswith("__MACOSX/") or info.filename.endswith(".DS_Store"):
                continue
            parts = info.filename.split("/", 1)
            if parts and parts[0]:
                roots.add(parts[0])
        strip_root = len(roots) == 1

        for info in valid_files:
            filename = info.filename

            # Skip macOS junk and directories
            if filename.startswith("__MACOSX/") or filename.endswith(".DS_Store") or filename.endswith("/"):
                continue

            # Optionally strip single root folder
            if strip_root:
                parts = filename.split("/", 1)
                if len(parts) == 2:
                    filename = parts[1]
                else:
                    filename = parts[0]

            out_path = os.path.join(dest_dir, filename)
            out_path = os.path.abspath(out_path)

            # Ensure extraction stays inside destination
            if not out_path.startswith(os.path.abspath(dest_dir)):
                # logger.warning("Skipping suspicious path: %s", filename)
                continue

            os.makedirs(os.path.dirname(out_path), exist_ok=True)

            with zf.open(info) as src, open(out_path, "wb") as dst:
                dst.write(src.read())

            logger.debug("Extracted: %s", out_path)

def run_compile(runtime_manager: RuntimeManager, cwd: str = "core/generated"):
    """Run compile script synchronously (wait for completion) and update status/logs."""
    script_path: str = "./scripts/compile.sh"

    build_state.status = BuildStatus.COMPILING
    build_state.log(f"[INFO] Starting compilation\n")

    def stream_output(pipe, prefix):
        for line in iter(pipe.readline, ''):
            msg = f"{prefix}{line}"
            build_state.log(msg)
        pipe.close()

    def wait_and_finish(proc: subprocess.Popen, step_name: str):
        exit_code = proc.wait()
        build_state.exit_code = exit_code
        if exit_code == 0:
            build_state.status = BuildStatus.SUCCESS
            build_state.log(f"[INFO] {step_name} finished successfully\n")
        else:
            build_state.status = BuildStatus.FAILED
            build_state.log(f"[ERROR] {step_name} failed (exit={exit_code})\n")

    # --- Compile step ---
    compile_proc = subprocess.Popen(
        ["bash", script_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    threading.Thread(target=stream_output, args=(compile_proc.stdout, ""), daemon=True).start()
    threading.Thread(target=stream_output, args=(compile_proc.stderr, "[ERROR] "), daemon=True).start()

    # Block until compile finishes
    wait_and_finish(compile_proc, "Build")

    # Stop PLC before cleanup
    runtime_manager.stop_plc()

    # --- Cleanup step ---
    cleanup_proc = subprocess.Popen(
        ["bash", "./scripts/compile-clean.sh"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    threading.Thread(target=stream_output, args=(cleanup_proc.stdout, ""), daemon=True).start()
    threading.Thread(target=stream_output, args=(cleanup_proc.stderr, "[ERROR] "), daemon=True).start()

    # Block until cleanup finishes
    wait_and_finish(cleanup_proc, "Cleanup")

    # Restart PLC only if everything succeeded
    if build_state.status == BuildStatus.SUCCESS:
        runtime_manager.start_plc()
    else:
        build_state.log("[WARNING] PLC program has not been updated because the build failed\n")
