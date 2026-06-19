# OpenPLC Runtime v4 — Concurrency & Synchronization Analysis

**Scope:** thread-safety of the multi-threaded IEC task model introduced by the
MatIEC → STruC++ migration (each IEC task now runs as its own pthread, replacing
MatIEC's single-threaded round-robin). Covers the locking design, scheduling and
priorities, and crash/lifecycle safety.

**Method:** source audit of the runtime (`core/src/plc_app`, `core/src/drivers`,
`core/strucpp_runtime`) plus four focused subsystem reviews. The central claims
(coarse global lock around task bodies, SCHED_FIFO priority mapping, crash handler)
were verified directly in `plc_state_manager.cpp`.

> **Important boundary:** a large part of the *actual* shared-variable locking lives
> in the STruC++-generated headers (`iec_threading.hpp`, `debug_dispatch.hpp`,
> `iec_std_lib.hpp`) that ship inside each program upload under
> `core/generated/strucpp_runtime/include/` — **they are NOT in this repo** and could
> not be audited. The runtime creates two mutexes and hands them to the loaded `.so`
> via `strucpp_set_locks()`, then trusts the generated code to use them. Any
> conclusion about whether shared *global* variables are correctly locked is therefore
> **partial** until those headers are reviewed (see §7).

---

## 1. Executive summary

The design is **safe-by-serialization, not safe-by-fine-grained-locking**. Every IEC
task body runs while holding a *single* global recursive mutex
(`g_image_tables_mutex`), so task bodies are **mutually exclusive** — two tasks can
never execute their logic concurrently. That choice eliminates the most dangerous
class of bug (task-vs-task races on shared globals and the I/O image) at the cost of:

- **no multicore parallelism** for IEC logic (the "one thread per task" gain is timing
  isolation/preemption, not throughput), and
- a **real-time priority-inversion bound**: a high-priority, short-interval task that
  becomes ready while a lower-priority task is mid-body **blocks for that task's entire
  scan time**. Priority inheritance (PI) mitigates but cannot remove this — the fast
  task's worst-case jitter is bounded by the *slowest* task's worst-case body time.

The serialization is sound **only inside the runtime's own task loop**. The two places
that reach the same memory from *other* threads — the **debug/editor path** and **native
plugin I/O** (EtherCAT) — **do not take the same lock**, so those are live race
windows. The lifecycle teardown is correct for a clean STOP but has **use-after-free
windows on the ERROR/crash path**.

Bottom line on the questions asked:
- *Is the global-variable locking correct and fail-proof?* For task-vs-task: yes, by
  coarse serialization (assuming the generated guards exist — unverified, §7). For
  task-vs-debug and task-vs-plugin: **no** — those paths use no lock or the wrong lock.
- *Are IEC task priorities considered?* Yes at the OS scheduler level (SCHED_FIFO 1–99),
  but with a **possible semantic inversion** and undermined by the global-lock blocking
  (§3).

---

## 2. The threading & locking architecture as built

**Task threads.** `plc_cycle_thread` (the "bootstrap" thread) walks the STruC++
`ConfigurationInstance → resources → tasks`, allocates `plc_tasks[]`, and spawns one
`plc_task_thread` per IEC task (`plc_state_manager.cpp:361-523`). Each task thread:

```c
// plc_state_manager.cpp:185-228 (verified)
while (plc_get_state() == PLC_STATE_RUNNING) {
    pthread_mutex_lock(image_tables_mutex());   // ONE global mutex, shared by ALL tasks
    ctx->holding_mutex = 1;
    scan_cycle_tracker_start(&ctx->tracker);
    if (ctx->is_fastest_task) plc_run_io_cycle_pre();   // journal apply + plugin cycle_start + phys input
    for (p ...) task->programs[p]->run();               // <-- entire body under the lock
    if (ctx->is_fastest_task) plc_run_io_cycle_post();  // advance_time + plugin cycle_end + phys output
    scan_cycle_tracker_end(&ctx->tracker);
    pthread_mutex_unlock(image_tables_mutex());
    ctx->holding_mutex = 0;
    ... clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, next_wakeup) ;  // lock NOT held while sleeping
}
```

**Two global mutexes** (`image_tables.cpp:88-90`), created by `init_recursive_pi_mutex`
(recursive + PRIO_INHERIT) in `symbols_init` and handed to the `.so`:

```c
ext_strucpp_set_locks(&g_image_tables_mutex, &g_global_vars_mutex);  // image_tables.cpp:210
```

- `g_image_tables_mutex` — taken by the runtime around the whole task body and around
  located-var binding/journal drain. **This is the only lock the runtime actually
  takes around shared IEC memory.**
- `g_global_vars_mutex` — **never locked anywhere in the runtime**. Its only uses are
  define/init/accessor/hand-off. All locking of it happens inside the generated `.so`
  (`iec_threading.hpp`, not in this repo).

**Consequence — no real parallelism, by design.** Because all task threads contend on
the same `image_tables_mutex`, their bodies cannot overlap. With staggered intervals
and scan ≪ interval they interleave fine; under contention (two tasks ready at once)
they serialize. This is the safe choice for a PLC full of shared globals, but it means:
the multi-threading buys deterministic per-task timing and preemption — **not** CPU
scaling — and it converts shared-memory races into lock-contention/latency.

---

## 3. Scheduling & priorities — are they honored?

**Yes, at the OS level** (`plc_task_thread`, `plc_state_manager.cpp:126-139`, verified):

```c
int rt = ctx->priority;           // IEC task priority used directly
if (rt < 1) rt = 1; if (rt > 99) rt = 99;
sp.sched_priority = rt;
pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp);   // falls back to default on failure
```

Findings:

1. **Possible priority-semantics inversion (MED, verify).** IEC 61131 convention is
   commonly *lower number = higher priority* (0 = highest). This code uses the value
   **directly** as the SCHED_FIFO level where *higher = higher*. If STruC++ emits
   `TaskInstance.priority` in IEC convention, the OS priorities are **backwards**. This
   must be checked against what the editor/STruC++ actually puts in `priority` — it's a
   one-line but high-impact correctness question. No remapping or `sched_get_priority_*`
   query is done; the [1,99] clamp is hard-coded.

2. **The global lock undermines priority (HIGH, architectural).** Even with correct
   SCHED_FIFO levels, when a high-priority task wakes it must take
   `image_tables_mutex`. If a lower-priority task holds it (mid-body), the high-priority
   task **blocks until the low-priority body finishes**. PI boosts the holder so it
   completes sooner, but the high-priority task's latency is still bounded by the
   *worst-case body time of any lower-priority task*. For hard-real-time guarantees this
   is the dominant limitation of the coarse-lock design.

3. **I/O & plugin housekeeping are anchored to the *fastest* task, not the
   highest-priority one** (`is_fastest_task` = shortest interval, tie-break higher
   priority then declaration order, `:446-460`). Physical I/O scan, journal apply, plugin
   `cycle_start/cycle_end`, and `strucpp_advance_time` all run only in that task's
   critical section. If the highest-priority task is not the fastest, its view of I/O is
   whatever the fastest task last published. Defensible, but worth documenting as a
   semantic.

4. **Fallback when not root / non-Linux:** `pthread_setschedparam` failure is logged and
   the task runs under default scheduling — correct degradation, but then *no* priority
   ordering exists at all (e.g., Windows/MSYS, or Linux without `CAP_SYS_NICE`).

---

## 4. Findings by severity

Severity reflects likelihood × impact in a normal deployment. "Contingent" means the
final severity depends on the un-audited generated headers (§7).

### HIGH

| ID | Issue | Evidence | Trigger |
|----|-------|----------|---------|
| H1 | **Debug/editor path touches live IEC variable memory with no runtime lock.** Force/read/write run on the `unix_socket` thread and call `ext_strucpp_debug_set/read` directly; the handler takes neither global mutex. | `debug_handler.c:153,211,292`; task lock at `plc_state_manager.cpp:191-215` | Editor forces/reads an 8-byte LWORD while a task reads/writes it → torn value. (Contingent: closed only if the `.so` thunks lock `image_tables_mutex` internally — unverified.) |
| H2 | **Native plugin I/O uses the WRONG lock.** The EtherCAT bus thread writes the IEC image directly (`*plc_ptr`, `memcpy`) under `driver->buffer_mutex`, which is a *different* object from the task threads' `image_tables_mutex`. The journal (lock-correct) path exists but EtherCAT bypasses it for cyclic I/O. | `plugin_driver.c:62,1004` (buffer_mutex); `ethercat_io.c:567,581`; task lock `image_tables.cpp:123-125` | EtherCAT master mapped to any IEC word; bus thread and a task overlap on that address → torn multi-byte image word. **Confirmed in-repo (not contingent).** |
| H3 | **Use-after-free on ERROR/crash teardown.** ERROR transitions (task crash, plugin-init failure, shutdown cleanup) do **not** set `is_transitioning`, so the debug gate doesn't block them. A debug command can load `ext_strucpp_debug_*` (plain pointer) and call into the `.so` while the detached `transition_worker` nulls those pointers / `dlclose`s the program. Also `image_tables_clear_null_pointers` **forgets to null `ext_strucpp_debug_write`** (dangling ptr into the unmapped `.so`). | `unix_socket.c:34-48`; `image_tables.cpp:424-428`; `plcapp_manager.c:72`; ERROR paths `plc_state_manager.cpp:167-178,323-343` | Task SIGSEGV → auto/operator stop → `dlclose` races the editor's in-flight `DEBUG:43…` poll → call through a function pointer into unmapped memory. |
| H4 | **Coarse-lock priority inversion / no parallelism** (architectural). See §2/§3.2. | `plc_state_manager.cpp:191-215` | High-priority fast task blocked for the full body time of a lower-priority task. |

### MEDIUM

| ID | Issue | Evidence |
|----|-------|----------|
| M1 | `g_global_vars_mutex` is never locked by the runtime; correctness of shared-global access depends entirely on the un-auditable generated `iec_threading.hpp` guards. The runtime cannot enforce it. | `image_tables.cpp` (no lock site) |
| M2 | `init_recursive_pi_mutex` **ignores** the `pthread_mutexattr_setprotocol` return → PI silently lost if unsupported. The IEC mutexes also use a *different, more lenient* init (recursive, error-ignoring) than the tracker/journal/plugin mutexes (`init_rt_mutex`, strict, non-recursive) — divergent contracts. | `image_tables.cpp:104` vs `utils/utils.c:129-150` |
| M3 | Recursive image mutex + crash handler does a **single** unlock. If the crash lands while the body holds the mutex recursively (depth > 1), one unlock leaves it locked → every other task and the next scan deadlock. | `plc_state_manager.cpp:169-173` |
| M4 | IEC→OS priority may be inverted; I/O anchored to fastest not highest-priority task (see §3.1, §3.3). | `plc_state_manager.cpp:126-138,446-460` |
| M5 | `request_plc_stop` returns immediately; the EtherCAT bus thread keeps writing the image (still `bus_running`) until joined ~one stop later — it doesn't self-gate I/O during the window. | `plugin_driver.c:167-176`; `ethercat_plugin.c:790-806,919-943` |
| M6 | If the **bootstrap** thread itself faults, the SCHED_FIFO task threads it spawned may not be joined before `pthread_join(plc_thread)` returns → orphaned task threads keep touching the image. | `plc_state_manager.cpp:323-343,486-489,690` |
| M7 | Journal `emergency_flush_locked` drops and re-takes its own mutex mid-append to honor image→journal order; the overflow path is fragile and order is enforced only by convention. | `journal_buffer.c:466-490` |
| M8 | `debugGetTrace` pre-checks frame space using `debug_size`, then trusts the byte count `n` the `.so` actually wrote. A `.so` that over-reports → stack `frame[4096]` overflow in the runtime. | `debug_handler.c:209-217` |
| M9 | EtherCAT `execute_command`/`status`/`diagnostics` iterate `g_masters[]`/`g_master_count` unlocked; `cleanup()` frees `g_masters`. Gated only by `is_transitioning`. | `ethercat_plugin.c:1477-1479,1950-2061` |

### LOW

| ID | Issue | Evidence |
|----|-------|----------|
| L1 | `scan_counter` is a plain `unsigned long`, `++` on the fastest task thread vs unsynchronized read on the debug thread → torn tick stamp (benign). | `plc_io_cycle.cpp:33`; `debug_handler.c:225` |
| L2 | `image_tables_clear_null_pointers` runs under the image mutex, but the debug thread never takes that mutex, so the lock gives no protection against a concurrent debug reader (feeds H3). | `image_tables.cpp:403-434` |
| L3 | `parse_hex_string` takes no destination bound; safe only because the token rule caps output < `debug_data[4096]` at the current `COMMAND_BUFFER_SIZE`. Latent overflow if buffer sizes change. | `utils/utils.c:152`; `unix_socket.c:236` |
| L4 | Unaligned `*(uint16_t*)&frame[pos] = 0xDEAD` in `debugGetMd5` — UB / fault risk on strict-alignment targets. | `debug_handler.c:350` |
| L5 | image↔journal lock order is documented in comments only, not enforced; a future caller taking journal-then-image would deadlock against the flush. | `journal_buffer.c`; `plc_io_cycle.cpp` |

### Non-concurrency notes worth flagging
- **RETAIN not implemented runtime-side.** The ABI reserves `getRetainVars()/getRetainCount()` but the runtime never calls them — RETAIN persistence appears unimplemented. (`strucpp_abi.hpp:66-77`; no callers.)
- **Stale MatIEC leftovers:** `scripts/generate-gluevars.sh` invokes a missing `xml2st`; `core/strucpp_runtime/README.md` cites the wrong path for `strucpp_abi.hpp`.

---

## 5. Is the global-variable locking correct and fail-proof?

**Within the runtime's task loop: yes, but coarsely.** A single recursive PI mutex held
around each entire task body makes task-vs-task access to the I/O image and (transitively)
shared globals mutually exclusive. That is genuinely safe and avoids whole categories of
race. The recursive attribute is needed because the fastest task re-enters lock-holding
code (journal apply, plugin cycle hooks) while already holding it.

**At the boundaries the runtime controls but didn't lock: no.** The two real hazards are
not subtle:
- the **debug path** (H1) reaches the same memory from the socket thread with no lock;
- **plugin cyclic I/O** (H2) reaches it under a *different* mutex (`buffer_mutex`) that no
  task thread ever holds — so plugin and task access are not serialized at all.

These aren't "depends on the generated code" — they're in this repo, and the locks to fix
them already exist (`image_tables_mutex()` is reachable from both paths).

**Fail-proofing gaps** that turn a fault into a hang or crash rather than a clean ERROR:
M2 (PI silently dropped), M3 (recursive-unlock deadlock on crash), H3/M6 (UAF /
orphan-thread on the ERROR path). The clean-STOP path is correctly ordered
(set STOPPED → join task threads → stop+join plugin threads → clear image → dlclose),
so the gaps are specifically on the *abnormal* paths.

---

## 6. Highest-value remediations (suggested, not yet applied)

1. **Make debug and plugin I/O take `image_tables_mutex`** (fixes H1, H2). Either route
   plugin cyclic I/O exclusively through the journal (already lock-correct), or make
   `mutex_take/buffer_mutex` *be* `image_tables_mutex`. Wrap the debug force/read/write
   dispatch in the same lock. This is the single biggest correctness win.
2. **Close the ERROR/crash UAF** (H3, M6): set the transition/teardown guard on *all*
   paths that lead to `dlclose` (not just socket-initiated STOP), fence the debug thread
   against symbol nulling, and null `ext_strucpp_debug_write`.
3. **Crash-handler recursive unlock** (M3): drain the mutex to fully unlocked (loop
   `pthread_mutex_unlock` until it reports not-owned) or track recursion depth.
4. **Resolve the priority semantics** (M4): confirm STruC++'s `priority` convention and
   invert if needed; query `sched_get_priority_min/max` instead of hard-coding [1,99].
5. **Honor `setprotocol` failure** (M2) and unify the mutex-init contract.
6. **Bound `debug_read`** by the pre-checked `var_size`, not the `.so`-returned `n` (M8).

(Glad to implement any subset — flag which and I'll branch + PR per your usual flow.)

---

## 7. The un-auditable boundary, and the STruC++ synthetic-program test

**What's missing to audit fully.** The shared-*global* locking and the debug-thunk
locking live in `iec_threading.hpp` / `debug_dispatch.hpp` / `iec_std_lib.hpp`, which
ship inside each program upload (`core/generated/strucpp_runtime/include/`) and are **not
in this repo**. Whether H1/M1 are live races or already-closed depends on those files.
Strong recommendation: **vendor or check those headers into review**, and regardless,
have the runtime lock at the boundaries it owns (debug, plugins) rather than trusting the
`.so`.

**Can we "run a large multi-task program through STruC++" locally? No — and here's
exactly why:**
- The **STruC++ compiler lives in the OpenPLC Editor**, not this repo. Nothing here, in
  `install.sh`, or in the Docker image downloads or bundles it. `scripts/compile.sh` only
  *consumes* STruC++ output (`generated.cpp/.hpp`, `defines.h`, the `strucpp_runtime/include/`
  headers) and `make`s it; it cannot generate them.
- `core/generated/` is empty; the only sample program in the tree is a **MatIEC-era** zip
  that `compile.sh:34-39` explicitly rejects.
- This host is macOS without `cmake`, and the scheduler relies on Linux-only primitives
  (`SCHED_FIFO`, `pthread_setschedparam`, `cpu_set_t`, `-lrt`) — it won't build/run here.

**What is feasible**, if you want empirical confirmation of the §2/§3 scheduling
behavior: hand-author a synthetic `core/generated/` (a `Configuration_CONFIG0` with
several `TaskInstance`s at different `interval_ns`/`priority`, a `LocatedVar[]` table, and
minimal ABI-matching headers), then build and run inside the Linux Docker image
(`scripts/run-image.sh` already passes `--cap-add=sys_nice --ulimit rtprio=99`). That would
let us *measure* the global-lock serialization and priority-inversion bound directly. The
caveat: it exercises hand-written stand-ins, not real STruC++ output, so it validates the
**runtime's** scheduling/locking behavior (which is what this report is about) but not the
generated guards. I can build this harness on request.

---

## 8. File reference

| Area | File:line |
|------|-----------|
| Per-task thread, SCHED_FIFO, body-under-lock, crash handler | `core/src/plc_app/plc_state_manager.cpp:119-235` |
| Task discovery/spawn, fastest-task pick, rollback | `plc_state_manager.cpp:237-523` |
| Clean STOP / unload ordering | `plc_state_manager.cpp:677-711` |
| Two global mutexes, init, set_locks | `core/src/plc_app/image_tables.cpp:88-110,199-210` |
| Located-var binding / null-fill / clear | `image_tables.cpp:238-357,377-434` |
| Lock injection into the .so | `core/strucpp_runtime/runtime_v4_entry.cpp:40-57` |
| Runtime-side ABI mirror | `core/src/lib/strucpp_abi.hpp` |
| Journal buffer (plugin-safe write path) | `core/src/plc_app/journal_buffer.c:69,130-133,426-490` |
| I/O cycle (fastest-task housekeeping) | `core/src/plc_app/plc_io_cycle.cpp:22-34` |
| Debug force/read/write (unlocked) | `core/src/plc_app/debug_handler.c:135-153,203-217,265-292,350` |
| Command socket dispatch + transition gate | `core/src/plc_app/unix_socket.c:34-48,68-105,169-260` |
| Plugin thunks (mutex_take/journal/debug/request_stop) | `core/src/drivers/plugin_driver.c:62,72-80,133-176,1004` |
| EtherCAT bus thread I/O (wrong-lock direct image write) | `core/src/drivers/plugins/native/ethercat/ethercat_io.c:460-584`; `ethercat_plugin.c:790-943` |
| RT mutex helper | `core/src/plc_app/utils/utils.c:129-150` |
| Stats tracker (correctly locked) | `core/src/plc_app/scan_cycle_manager.c` |
