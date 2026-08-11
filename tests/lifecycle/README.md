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

Run one group at a time with `SUITE_ONLY=crash|shutdown|phantom|switch|misc`, and
get a backtrace out of a crash with `SUITE_GDB=1` (needs `gdb` in the image).

## What the pieces are for

| File | Why it exists |
|---|---|
| `suite.py` | The tests. One group per review finding, each with an oracle that fails on the unfixed code. |
| `fakevpp_plugin.c` | Stands in for a board's VPP package: a configurable sleep in `init()`/`stop_loop()` widens a transition enough to aim at, and a file-driven mode switch (`/tmp/fakevpp_switch`, "RUN"/"STOP") drives `set_switch_position` + `request_plc_start`/`request_plc_stop` the way real hardware does. |
| `failinject.c` | `LD_PRELOAD` shim that fails exactly one `pthread_create`, one-shot and self-disarming, so the "transition worker could not be spawned" path is reachable without inducing real thread exhaustion. |
| `logserver.py` | Accepts `/run/runtime/log_runtime.socket`. Without something listening there, `--print-logs` produces nothing on stdout either and every journal assertion silently passes. |
| `harness.sh` | Builds the runtime, the fixtures and the program, writes `plugins.conf`, then runs the suite. |

## Two traps worth knowing before editing these

**Keep the Python plugins in `plugins.conf`.** They stay disabled, but *loading*
them is what initialises the interpreter, and `has_python_plugin &&
Py_IsInitialized()` is the precondition for the whole class of shutdown bugs
around `Py_FinalizeEx`. A conf containing only the native fixture makes that class
untestable while every test still passes.

**Never start a thread in a plugin's `init()`.** The contract says so, and the
reason is concrete: `plugin_driver_update_config` rebuilds every slot on each
start, which `dlclose`s the `.so`, so a thread left running from `init()` returns
into an unmapped page. This fixture segfaulted that way before it was written to
use `start_loop`/`stop_loop`.

## Coverage, and what is still argued rather than observed

Covered: graceful shutdown with the PLC never started (both signals); shutdown
during a start; a stop whose transition worker cannot be spawned; a mode-switch
flip during a teardown; `SWITCH` answered mid-transition; and 25 rapid start/stop
pairs as a wedge guard.

Not covered:

- **The re-arm branch of switch reconciliation.** The tests prove a flip during a
  transition is honoured, but the branch that hands the movement record back only
  runs when the corrective transition is *refused* by a request that slipped in
  first — a race that cannot be forced without a hook in the runtime.
- **The watchdog forcing ERROR on a stuck transition.** Needs a transition that
  outlives `PLC_TRANSITION_STUCK_TIMEOUT_MS` (2 minutes), so it belongs in a slow
  opt-in run rather than here.
- **A runaway IEC task.** Terminating one needs the forced-abort ladder that does
  not exist yet; today a program with an unbounded loop wedges the stop.
