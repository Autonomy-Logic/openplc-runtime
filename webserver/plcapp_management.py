from dataclasses import dataclass, field
from enum import Enum, auto
import os
import shutil
import time
import zipfile
import subprocess
import threading
import glob
from typing import Final

from webserver.runtimemanager import RuntimeManager
from webserver.logger import get_logger, LogParser
from webserver.plugin_config_model import PluginsConfiguration, PluginConfig, PluginType

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

            # Normalize path separators for cross-platform compatibility (Windows \ to Unix /)
            filename = filename.replace('\\', '/')

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


def update_plugin_configurations(generated_dir: str = "core/generated"):
    """
    Update plugin configurations based on available config files.
    
    Scans generated/conf/ for config files, copies them to plugin directories,
    and updates plugins.conf to enable/disable plugins accordingly.
    """
    plugins_conf_path = "plugins.conf"
    conf_dir = os.path.join(generated_dir, "conf")

    build_state.log(f"[DEBUG] update_plugin_configurations called with generated_dir='{generated_dir}'\n")
    build_state.log(f"[DEBUG] Looking for config files in: {conf_dir}\n")

    # Load current plugin configuration using the dataclass
    plugins_config = PluginsConfiguration.from_file(plugins_conf_path)
    build_state.log(f"[DEBUG] Loaded {len(plugins_config.plugins)} plugins from {plugins_conf_path}\n")
    
    # Log initial state
    for plugin in plugins_config.plugins:
        build_state.log(f"[DEBUG] Initial state - {plugin.name}: enabled={plugin.enabled}, config_path='{plugin.config_path}'\n")

    # Check if conf directory exists
    if not os.path.exists(conf_dir):
        build_state.log(f"[INFO] No conf directory found in {generated_dir}, disabling all plugins\n")
        # When there's no conf directory, disable all currently enabled plugins
        plugins_updated = 0
        update_messages = []
        for plugin in plugins_config.plugins:
            if plugin.enabled:
                plugin.enabled = False
                plugins_updated += 1
                update_messages.append(f"Disabled plugin '{plugin.name}' (no conf directory found)")
        
        # Log the updates
        build_state.log(f"[INFO] Found 0 config files (no conf directory): []\n")
        
        for message in update_messages:
            build_state.log(f"[INFO] {message}\n")
    else:
        # Process config files normally when conf directory exists
        # Use the utility method to update plugins based on available config files
        # Copy config files to plugin directories instead of referencing them directly
        plugins_updated, update_messages = plugins_config.update_plugins_from_config_dir(conf_dir, copy_to_plugin_dirs=True)
        
        # Log the updates
        config_files = glob.glob(os.path.join(conf_dir, "*.json"))
        available_configs = {os.path.splitext(os.path.basename(f))[0]: f for f in config_files}
        build_state.log(f"[INFO] Found {len(available_configs)} config files in {conf_dir}: {list(available_configs.keys())}\n")
        
        for message in update_messages:
            if "Copied config file" in message:
                build_state.log(f"[INFO] {message}\n")
            elif "Enabled plugin" in message or "Disabled plugin" in message:
                build_state.log(f"[INFO] {message}\n")
            else:
                build_state.log(f"[WARNING] {message}\n")

    # VPP plugins are handled separately via vpp_plugins.conf — see
    # apply_vpp_plugin_conf(). Nothing to do here for VPP.

    # Save the updated configuration
    if plugins_config.to_file(plugins_conf_path):
        build_state.log(f"[INFO] Plugin configuration update complete. {plugins_updated} plugins updated.\n")
        
        # Log final state
        for plugin in plugins_config.plugins:
            build_state.log(f"[DEBUG] Final state - {plugin.name}: enabled={plugin.enabled}, config_path='{plugin.config_path}'\n")
        
        # Log configuration summary
        summary = plugins_config.get_config_summary()
        build_state.log(f"[INFO] Plugin summary: {summary['enabled']}/{summary['total']} enabled "
                       f"({summary['python']} Python, {summary['native']} Native)\n")
        
        # Validate configurations and log any issues
        issues = plugins_config.validate_plugins()
        if issues:
            build_state.log("[WARNING] Plugin validation issues found:\n")
            for issue in issues:
                build_state.log(f"[WARNING] {issue}\n")
    else:
        build_state.log("[ERROR] Failed to save updated plugin configuration\n")


def _wait_for_plc_idle(runtime_manager: RuntimeManager, timeout_s: float) -> bool:
    """Poll status_plc() until the runtime is NOT in a transition.

    The runtime reports STATUS:TRANSITIONING while a stop/start worker
    is in flight (plc_state has flipped but the unload/load work hasn't
    completed). Any other STATUS (STOPPED, EMPTY, INIT, RUNNING) means
    the runtime is settled and ready to accept the next command.

    Used before the cleanup script runs, so the .so move doesn't race
    against an in-flight unload, and before sending START, so the
    runtime can actually process the command instead of returning
    COMMAND:BUSY. Tolerates the case where the PLC was never started
    (state == INIT or EMPTY) — those return immediately since there's
    no transition to wait for.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = runtime_manager.status_plc()
        if resp and "TRANSITIONING" not in resp.upper():
            return True
        time.sleep(0.1)
    return False


def apply_vpp_plugin_conf(generated_dir: str = "core/generated") -> None:
    """Apply or remove the VPP plugin configuration for this upload.

    VPP plugins are fully owned by the editor: it sends a
    ``vpp_plugins.conf`` alongside the program when the target is a VPP
    board, and omits it for vanilla builds.  This function is the single
    authoritative gate:

    * **Upload includes vpp_plugins.conf** → copy it to the runtime root
      so the C-side plugin loader picks it up at the next PLC start.
      Also copy each plugin's JSON config from ``conf/`` into the VPP
      build output directory (``build/vpp/``) so the .so can read it
      from the stable location listed in vpp_plugins.conf.

    * **Upload does not include vpp_plugins.conf** → delete any existing
      ``vpp_plugins.conf`` from the runtime root.  This ensures a
      vanilla upload never inadvertently loads a VPP driver left over
      from a previous project, regardless of what .so files exist in
      ``build/vpp/``.
    """
    VPP_CONF_DEST = "vpp_plugins.conf"
    VPP_BUILD_DIR = "build/vpp"
    uploaded_conf = os.path.join(generated_dir, "vpp_plugins.conf")

    if os.path.exists(uploaded_conf):
        # Copy vpp_plugins.conf to runtime root
        shutil.copy2(uploaded_conf, VPP_CONF_DEST)
        build_state.log(f"[INFO] VPP: installed vpp_plugins.conf from upload\n")

        # Copy each VPP plugin's config file to the path declared in
        # vpp_plugins.conf (the config_path field). That field is the
        # single source of truth for where the .so will look for its
        # config at runtime — use it directly rather than constructing
        # a separate destination.
        conf_dir = os.path.join(generated_dir, "conf")
        vpp_conf_plugins = PluginsConfiguration.from_file(VPP_CONF_DEST)
        runtime_root = os.path.abspath(".")
        for p in vpp_conf_plugins.plugins:
            if not p.config_path:
                continue
            src_config = os.path.join(conf_dir, f"{p.name}.json")
            if not os.path.exists(src_config):
                build_state.log(f"[WARNING] VPP: conf/{p.name}.json not found in upload, skipping\n")
                continue
            dest_config = os.path.normpath(p.config_path)
            # Guard against path traversal in editor-generated vpp_plugins.conf
            if not os.path.abspath(dest_config).startswith(runtime_root):
                build_state.log(f"[WARNING] VPP: config_path '{p.config_path}' escapes runtime root, skipping\n")
                continue
            os.makedirs(os.path.dirname(dest_config), exist_ok=True)
            shutil.copy2(src_config, dest_config)
            build_state.log(f"[INFO] VPP: copied {p.name}.json to {dest_config}\n")
    else:
        # No VPP in this upload — remove any stale vpp_plugins.conf so
        # the plugin loader does not attempt to load old VPP drivers.
        if os.path.exists(VPP_CONF_DEST):
            os.remove(VPP_CONF_DEST)
            build_state.log("[INFO] VPP: removed stale vpp_plugins.conf (no VPP in upload)\n")


def run_compile(runtime_manager: RuntimeManager, cwd: str = "core/generated", clean: bool = False):
    """Run compile script synchronously (wait for completion) and update status/logs.

    When ``clean=True`` (editor's "Clean build and upload" option), wipe the
    Make-managed ``core/build/`` directory and clear the ccache contents
    before invoking compile.sh. This forces a full recompile from scratch
    even when source content hashes match the cached objects — useful when
    the user suspects a stale or corrupted cache.
    """
    script_path: str = "./scripts/compile.sh"

    def stream_output(pipe, prefix):
        # If the drainer dies mid-stream (e.g. UnicodeDecodeError on a
        # non-UTF8 byte from g++ stderr, transient I/O error), the
        # subprocess eventually fills its pipe buffer and blocks on its
        # next write — which means compile_proc.wait() never returns and
        # the build is silently stuck in COMPILING forever. Catch here so
        # the operator sees the drainer error, and the finally{} pipe
        # close lets the child see EOF instead of blocking.
        try:
            for line in iter(pipe.readline, ''):
                msg = f"{prefix}{line}"
                build_state.log(msg)
        except Exception as e:
            build_state.log(f"[WARNING] Log drainer crashed: {e}\n")
        finally:
            pipe.close()

    def wait_step(proc: subprocess.Popen, step_name: str) -> bool:
        """Wait for a subprocess and log the result. Returns True on
        zero-exit. Caller is responsible for combining results — does
        NOT mutate build_state.status, since that's only updated once
        after every step (compile + cleanup) completes, so a successful
        compile isn't masked by a failed cleanup or vice-versa."""
        exit_code = proc.wait()
        if exit_code == 0:
            build_state.log(f"[INFO] {step_name} finished successfully\n")
            return True
        build_state.log(f"[ERROR] {step_name} failed (exit={exit_code})\n")
        return False

    # Wrap the entire orchestration body in try/except so any unhandled
    # exception flips status to FAILED instead of leaving it pinned at
    # COMPILING. Without this guard, a raise inside run_compile (e.g.
    # Popen FileNotFoundError on a missing script, OSError on a
    # full disk, or the inner update_plugin_configurations catch
    # re-raising) propagates out of the daemon thread that
    # run_compile is invoked on. The thread dies silently and the
    # /api/compilation-status endpoint keeps reporting COMPILING
    # indefinitely; the editor only recovers via its TCP
    # connection-timeout safety net several minutes later (after the
    # runtime stops responding at the network layer for unrelated
    # reasons). Catching here makes the runtime fail-closed: any crash
    # transitions to a terminal status the editor can observe on its
    # next poll.
    try:
        build_state.status = BuildStatus.COMPILING
        build_state.log(f"[INFO] Starting compilation\n")

        # --- Optional clean step ---
        if clean:
            build_state.log("[INFO] Clean build requested — wiping core/build/ and ccache\n")
            # Wipe the per-project object cache. shutil.rmtree avoids needing
            # `make clean` (which would require the Makefile to be in cwd).
            build_dir = "core/build"
            if os.path.exists(build_dir):
                try:
                    shutil.rmtree(build_dir)
                except OSError as e:
                    build_state.log(f"[WARNING] Failed to remove {build_dir}: {e}\n")
            # Wipe ccache. Failures here are non-fatal — `ccache -C` returning
            # non-zero (e.g. ccache not installed) shouldn't abort the build,
            # since the build folder wipe alone already invalidates per-file
            # caches that live in build/.
            try:
                ccache_proc = subprocess.run(
                    ["ccache", "-C"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if ccache_proc.returncode == 0:
                    build_state.log("[INFO] ccache cleared\n")
                else:
                    build_state.log(
                        f"[WARNING] ccache -C exited {ccache_proc.returncode}: "
                        f"{ccache_proc.stderr.strip() or 'no error output'}\n"
                    )
            except FileNotFoundError:
                build_state.log("[INFO] ccache not installed — skipping cache wipe\n")

        # --- Compile step ---
        # errors='replace' so a non-UTF8 byte in g++ stderr (rare with
        # the C locale but possible with i18n locales or files that
        # carry non-UTF8 identifiers) is replaced with U+FFFD instead of
        # raising UnicodeDecodeError inside the drainer thread.
        compile_proc = subprocess.Popen(
            ["bash", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors='replace',
            bufsize=1
        )

        threading.Thread(target=stream_output, args=(compile_proc.stdout, ""), daemon=True).start()
        threading.Thread(target=stream_output, args=(compile_proc.stderr, "[ERROR] "), daemon=True).start()

        # Block until compile finishes.
        compile_ok = wait_step(compile_proc, "Build")

        # Stop the running PLC before swapping the .so. stop_plc() returns
        # as soon as the runtime ACKs over the socket, but the actual task
        # / plugin / .so teardown continues asynchronously — wait for the
        # runtime to settle (not in a transition) before letting the
        # cleanup script touch build/new_libplc.so. Otherwise the new .so
        # could be moved into place (or the old one held open) while
        # teardown is still in progress. _wait_for_plc_idle returns
        # immediately for the "PLC was never started" case (state == INIT
        # / EMPTY) — there's no transition to wait for.
        runtime_manager.stop_plc()
        if not _wait_for_plc_idle(runtime_manager, timeout_s=30.0):
            build_state.log(
                "[WARNING] Runtime stayed in TRANSITIONING for 30s; "
                "proceeding with cleanup anyway\n"
            )

        # --- Cleanup step ---
        cleanup_proc = subprocess.Popen(
            ["bash", "./scripts/compile-clean.sh"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors='replace',
            bufsize=1
        )

        threading.Thread(target=stream_output, args=(cleanup_proc.stdout, ""), daemon=True).start()
        threading.Thread(target=stream_output, args=(cleanup_proc.stderr, "[ERROR] "), daemon=True).start()

        cleanup_ok = wait_step(cleanup_proc, "Cleanup")

        # Update build_state.status from the COMBINED result. Previously,
        # only the cleanup result mattered (the second wait_and_finish
        # overwrote whatever the compile set), so a failed compile + a
        # successful cleanup would have been reported as SUCCESS, and a
        # successful compile + a failed cleanup as FAILED — neither
        # matches what actually happened.
        if compile_ok and cleanup_ok:
            build_state.status = BuildStatus.SUCCESS
            build_state.exit_code = 0
        else:
            build_state.status = BuildStatus.FAILED
            build_state.exit_code = 1

        if build_state.status == BuildStatus.SUCCESS:
            # Re-run plugin configuration now that compile.sh has produced any
            # VPP plugin .so files. The pre-compile call at upload time can only
            # register pre-built plugins; VPP plugins are compiled on-target
            # during run_compile, so their entries in plugins.conf have to be
            # written after the compile step succeeds.
            #
            # Hold status back in COMPILING while we finalize plugins.conf so
            # the editor doesn't poll SUCCESS and send START before the VPP
            # plugin entry is written.
            #
            # The inner try/except is kept (instead of relying on the outer
            # guard) so update_plugin_configurations failures produce a
            # specific log line that operators can grep for, while still
            # flipping status to FAILED.
            build_state.status = BuildStatus.COMPILING
            try:
                update_plugin_configurations(cwd)
                build_state.status = BuildStatus.SUCCESS
                # Reset crash tracking after a successful build — the program
                # changed, so any previous crash pattern no longer applies. Do
                # NOT auto-start the PLC here: the editor is responsible for
                # sending START once it has confirmed a clean build, which
                # gives it control over retries when the previous STOP
                # transition is still finishing (COMMAND:BUSY window).
                runtime_manager.reset_crash_tracking()
            except Exception as e:
                build_state.log(f"[ERROR] Failed to update plugin configurations: {e}\n")
                build_state.status = BuildStatus.FAILED
                build_state.exit_code = 1
        else:
            build_state.log("[WARNING] PLC program has not been updated because the build failed\n")
    except Exception as e:
        # Outer fail-closed guard. exit_code = -1 distinguishes an
        # orchestrator crash from a build-step non-zero exit (which uses
        # exit_code = 1).
        build_state.log(f"[ERROR] Compile orchestrator crashed: {e}\n")
        build_state.status = BuildStatus.FAILED
        build_state.exit_code = -1
