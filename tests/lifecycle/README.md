# Lifecycle tests

End-to-end tests for run/stop state control: what happens when a signal, a
command, or a mode-switch flip lands in the middle of a state change.

These are not unit tests. Each one boots a real `plc_main` with a real compiled
PLC program, drives it over its command socket, and judges it by what an outside
observer can see — socket replies, the journal, and `/proc/<pid>/maps`. That
matters because the bugs in this area are not wrong return values; they are a
state that claims a transition finished while the work is still running. Only the
mapping tells you whether a reported stop actually unloaded anything.

## Running

Needs Linux (`SCHED_FIFO`, `sem_init`, `/proc`). On macOS, run it in the project's
container image:

```bash
docker run --rm --cap-add=sys_nice \
  -v "$PWD":/src:ro \
  -v /path/to/editor/payload:/payload:ro \
  -v "$PWD/tests/lifecycle":/fixtures:ro \
  openplc-runtime-runstop:test bash /fixtures/harness.sh
```

`--cap-add=sys_nice` is required for the runtime's real-time scheduling.

`/payload` is a directory of editor-generated Runtime v4 sources — the contents of
`<project>/build/OpenPLC Runtime v4/src`. The harness copies it to
`core/generated` and compiles it with `scripts/compile.sh`, so any program works.
Payloads from editor builds before `defines.h` existed need one added next to the
sources, since `Makefile.strucpp` lists it as an explicit prerequisite:

```c
#define PROGRAM_MD5 "<md5 of program.st>"
```

Run one group at a time with `SUITE_ONLY=crash|shutdown|phantom|switch|misc|bootswitch|stopfail|keepalive|ptrprobe|gate|bootgate`, and
get a backtrace out of a crash with `SUITE_GDB=1` (needs `gdb` in the image).

## What the pieces are for

| File | Why it exists |
|---|---|
| `suite.py` | The tests. One group per review finding, each with an oracle that fails on the unfixed code. |
| `fakevpp_plugin.c` | Stands in for a board's VPP package: a configurable sleep in `init()`/`stop_loop()` widens a transition enough to aim at, and a file-driven mode switch (`/tmp/fakevpp_switch`, "RUN"/"STOP") drives `set_switch_position` + `request_plc_start`/`request_plc_stop` the way real hardware does. It exports `detach_image`, so by default it stays alive across a stop with its watcher running; `FAKEVPP_FALLBACK=1` makes `detach_image` refuse with -1, and the `-DFAKEVPP_LEGACY` build (`libfakevpp_legacy_plugin.so`) does not export it. Both fallbacks are cleaned up on the stop and log `fakevpp: cleanup`. It also exports `owns_mode_switch`, which returns 1 with `FAKEVPP_SWITCH_OWNER=1` or `FAKEVPP_SWITCH_SILENT=1` and is absent from the legacy build; `FAKEVPP_SWITCH_SILENT=1` never reports a position, requests nothing and logs `fakevpp: switch silent, no position reported`. When the switch file reads "SILENT", `init()` likewise reports no position, requests nothing and logs that line, and the watcher ignores it and keeps its last position. `FAKEVPP_CLEANUP_MS` (default 0) makes `cleanup()` sleep that many ms after logging `fakevpp: cleanup`, which widens a rollback. |
| `failinject.c` | `LD_PRELOAD` shim that fails exactly one `pthread_create`, one-shot and self-disarming, so the "transition worker could not be spawned" path is reachable without inducing real thread exhaustion. |
| `stopfail/` | A Python plugin whose `stop_loop` returns False, or raises with `STOPFAIL_MODE=raise`, while its worker thread keeps running until `cleanup()`. With `STOPFAIL_MODE=none` it stops and joins the worker, then returns None. Only the `stopfail` group adds it to `plugins.conf`, loaded from `/fixtures`. |
| `plugin_detach.h` | Shared by the native fixtures: `plugin_args_drop_image()` clears every image table, image lock, journal and debug entry point of an args copy, and `plugin_args_hold_image()` reports whether a table pointer remains. |
| `ptrprobe.c` | A native plugin that logs `ptrprobe: image=0x<pointer>` after each `init()` and `ptrprobe: image=none` when `detach_image` has dropped its pointers. Only the `ptrprobe` group adds it to `plugins.conf`. |
| `pydetach/` | A Python plugin whose `detach_image()` returns True; it logs `pydetach: init`, `pydetach: detached` and `pydetach: cleanup`. With `PYDETACH_OWNS_SWITCH=1` its `owns_mode_switch()` returns True and it never reports a position. Only the `keepalive` and `gate` groups add it to `plugins.conf`, loaded from `/fixtures`. |
| `logserver.py` | Accepts `/run/runtime/log_runtime.socket`. Without something listening there, `--print-logs` produces nothing on stdout either and every journal assertion silently passes. |
| `harness.sh` | Builds the runtime, the fixtures and the program, writes `plugins.conf`, then runs the suite. |

## Two traps worth knowing before editing these

**Keep the Python plugins in `plugins.conf`.** They stay disabled, but *loading*
them is what initialises the interpreter, and `has_python_plugin &&
Py_IsInitialized()` is the precondition for the whole class of shutdown bugs
around `Py_FinalizeEx`. A conf containing only the native fixture makes that class
untestable while every test still passes.

**Never start a thread in a plugin's `init()` that `cleanup()` does not join.**
`plugin_driver_update_config` rebuilds every slot on each start, which `dlclose`s
the `.so`, so a thread left running from `init()` returns into an unmapped page.
This fixture segfaulted that way before it was written to use
`start_loop`/`stop_loop`. A thread that `cleanup()` joins is safe, because
`cleanup()` runs before the `dlclose`: `FAKEVPP_SWITCH_OWNER=1` watches the switch
that way, as the switch contract in `plugin_types.h` asks of a board package. On a
stop that thread survives only while the plugin detaches from the I/O image
(`detach_image`); with `FAKEVPP_FALLBACK=1` or the legacy build the stop runs
`cleanup()` and joins it, and the switch goes unread until the next start.

## Coverage, and what is still argued rather than observed

Covered: graceful shutdown with the PLC never started (both signals); shutdown
during a start; a stop whose transition worker cannot be spawned; a mode-switch
flip during a teardown; `SWITCH` answered mid-transition; and 25 rapid start/stop
pairs as a wedge guard; power-on with the switch owner shipped in
`vpp_plugins.conf`, in STOP (left stopped, program never mapped, then started by a
flip to RUN) and in RUN (exactly one start); a Python plugin whose `stop_loop`
returns False or raises (the stop lands ERROR, the I/O image and program library are
kept, START answers `START:ERROR`, and SIGTERM exits without releasing the image); and
one whose `stop_loop` returns None, which stops normally (the stop lands STOPPED, the
image is released, the program is unmapped, and the next START is accepted); the
keep-alive contract, where a switch owner that detaches from the I/O image on a STOP
keeps reading the switch and a flip to RUN starts the PLC with no socket START, a
START after the switch moved to STOP while stopped answers `START:ERROR_SWITCH_STOP`
with no scan, a refusing `detach_image` and a build without it are cleaned up on the
stop, and a Python plugin that detaches is cleaned up and initialised again only at
the next START; and 50 STOP/START cycles in which a surviving plugin drops its image
pointers before every `[image_tables] cleared all pointers` and holds a non-null
image after every START; and the start gate (`gate`, `bootgate`), where no start runs
a scan while the switch reads STOP or a switch owner has not reported since its last
init: a current STOP report refuses START before the transition, a fallback owner
(legacy build, or `FAKEVPP_FALLBACK=1` with `owns_mode_switch`) that reports STOP
during the start's re-init answers `START:ERROR_SWITCH_STOP` with no scan and no
correction, the legacy owner reads `SWITCH:UNKNOWN` after a stop and a START in RUN
runs it again, a runtime with no owner starts as before and reads `SWITCH:RUN`, a
declared owner that never reports (native or Python, alone or next to an owner that
reported RUN) refuses START with `START:ERROR_SWITCH_UNKNOWN` after the 2 s
switch-report wait, and the same owner holds the boot auto-start with the PLC never
loaded; and, with a switch owner registered, socket START answers the decision of the
start's switch gate after the plugin re-init, so a keep-alive owner that reports STOP or
stays silent during the re-init refuses START with `START:ERROR_SWITCH_STOP` or
`START:ERROR_SWITCH_UNKNOWN` rather than `START:OK`, and a refusal is answered before the
rollback finishes (`STATUS:TRANSITIONING` right after the reply, then STOPPED).

Not covered:

- **The re-arm branch of switch reconciliation.** The tests prove a flip during a
  transition is honoured, but the branch that hands the movement record back only
  runs when the corrective transition is *refused* by a request that slipped in
  first — a race that cannot be forced without a hook in the runtime.
- **The watchdog forcing ERROR on a stuck start.** Needs a start that outlives
  `PLC_TRANSITION_STUCK_TIMEOUT_MS` (2 minutes), so it belongs in a slow opt-in run
  rather than here.
- **A runaway IEC task.** The stuck-task watchdog (see "Watchdog System" in
  `docs/ARCHITECTURE.md`) is not exercised here; its tests are the host tests under
  `tests/host/` and end-to-end runs against real uploaded programs.
