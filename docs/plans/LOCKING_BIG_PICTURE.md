# OpenPLC v4 + STruC++ — The Locking Big Picture

Cross-repo synthesis of every lock in the multi-threaded IEC runtime, how each is
used, and where the gaps are. Companion to `CONCURRENCY_ANALYSIS_REPORT.md` (which
lists per-issue severities); this document is the *map*.

Repos:
- **runtime** = `openplc-runtime` (the C/C++ executable that loads the program `.so`)
- **strucpp** = `/Users/thiagoralves/Documents/Code/strucpp` (the compiler; emits the
  `.so` C++ and ships the `src/runtime/include/` headers inside each upload)

---

## 0. The one crucial insight

> **The big `image_tables_mutex` is silently doing the job of *two* locks.** It is the
> I/O-image lock **and**, because it is held around each task's *entire* body, it is
> also the de-facto global-variable lock. STruC++ generates **no locks at all**, and
> the `g_global_vars_mutex` the runtime creates is **never acquired by anyone**. So the
> only thing keeping shared globals and the I/O image race-free today is that the big
> lock serializes whole task bodies.

Consequence: **the two concerns are the same problem.** You cannot "remove the big lock
to get parallelism" (concern #1) without *first* giving globals their own real lock
(concern #2) — otherwise narrowing the big lock leaves globals, located vars, and
the image with **no protection whatsoever**, because nothing else locks them.

And the design you described ("one global lock, taken only around global access, skipped
if a POU has no globals") **does not exist in the codebase**. The runtime allocates the
mutex and hands it to the `.so` via `strucpp_set_locks()`, but STruC++ never references
it — the consumer (`iec_threading.hpp`) was never written.

---

## 1. Lock inventory (both repos)

| # | Lock | Where created | Type | What it *actually* protects today | Status |
|---|------|---------------|------|-----------------------------------|--------|
| 1 | `g_image_tables_mutex` | runtime `image_tables.cpp:88` | recursive + PI | Held around the **entire** task body (`plc_state_manager.cpp:191-215`) → I/O image **and**, transitively, all globals/located vars touched in the body. Also guards located-var binding and journal drain. | **Load-bearing.** The real lock. |
| 2 | `g_global_vars_mutex` | runtime `image_tables.cpp:89`, handed to `.so` via `strucpp_set_locks` (`:210`) | recursive + PI | **Nothing.** Never locked by the runtime; never referenced by STruC++. The `iec_threading.hpp` guards that were meant to use it do not exist. | **Vestigial / dead.** |
| 3 | journal mutex | runtime `journal_buffer.c:69` | PI, non-recursive | The journal ring (`g_entries[]`, count, seq). Plugins append; drained on the fastest task **under lock #1**. | Works (plugin write path). |
| 4 | `plugin_driver.buffer_mutex` | runtime `plugin_driver.c:62` | PI, non-recursive | Handed to plugins as `buffer_mutex`; `mutex_take/give` wrap it. EtherCAT uses **this** for *direct* image writes. **Different object from lock #1** → no mutual exclusion with task threads. | **Wrong lock** (see §4). |
| 5 | per-task tracker mutex | runtime `scan_cycle_manager.c:46` | PI, non-recursive | One task's timing stats. STATS reader also takes a tasks-array reader lock. | Correct. |
| 6 | `state_mutex` | runtime `plc_state_manager.cpp` | plain | `plc_state` enum. | Correct. |
| 7 | `is_transitioning` (CAS flag) | runtime `unix_socket.c:68-105` | atomic flag | Gates socket commands during a STOP/START transition. Not set on ERROR/crash paths. | Partial (UAF gap). |
| 8 | EtherCAT `slaves_mutex`, `soem_lock` | plugin `ethercat_plugin.c` | PI | Plugin-internal slave snapshot / SOEM calls. | Correct (plugin-internal). |
| — | **STruC++ side: anything** | — | — | **Nothing.** No mutex/atomic/`volatile`/fence in `src/runtime/include/` or codegen. `IECVar` = `value_ + bool forced_ + forced_value_`, all plain. `force()` = 3 non-atomic writes (`iec_var.hpp:161-165`). | **No locking emitted.** |

---

## 2. Thread map

```
                         OpenPLC runtime process
 ┌───────────────────────────────────────────────────────────────────────────┐
 │                                                                             │
 │  bootstrap thread (plc_cycle_thread)                                        │
 │    └─ spawns one pthread PER IEC TASK ──────────────┐                       │
 │                                                     ▼                       │
 │   ┌──────────────┐ ┌──────────────┐ ┌──────────────┐   each = SCHED_FIFO,  │
 │   │ task A (5ms) │ │ task B (20ms)│ │ task C (100ms)│   prio = task.priority│
 │   │ prio 50      │ │ prio 30      │ │ prio 10       │   (clamped 1..99)     │
 │   └──────┬───────┘ └──────┬───────┘ └──────┬───────┘                        │
 │          │  all three lock the SAME #1 around their whole body              │
 │          └─────────────┬──┴───────────────┘                                 │
 │                        ▼                                                     │
 │              [ g_image_tables_mutex #1 ]  ◄── only ONE task body runs at a   │
 │                        │                       time → NO real parallelism    │
 │   fastest task (A) ALSO runs, still under #1:                               │
 │     plc_run_io_cycle_pre/post → journal drain (#3) + plugin cycle hooks      │
 │                                                                             │
 │  unix-socket thread ── debug force/read/write ──► IECVar storage  (NO LOCK) │
 │                     └─ STATS / plugin cmds                                   │
 │                                                                             │
 │  plugin threads (e.g. EtherCAT bus thread) ── direct image R/W ──► IECVar   │
 │                     under buffer_mutex #4  (NOT #1 → races task threads)     │
 └───────────────────────────────────────────────────────────────────────────┘

   Shared memory everyone points at = the program .so's IECVar storage
   (located vars bound via raw_ptr(); globals as plain fields).
```

---

## 3. Access matrix — who touches shared state, and under which lock

Legend: ✅ correctly serialized · ⚠️ safe only incidentally · ❌ unsynchronized race

| Shared state → / Accessor ↓ | I/O image (located `%I/%Q/%M`) | Global vars (`VAR_GLOBAL`) | Journal ring |
|---|---|---|---|
| **IEC task body** (any task) | ✅ under #1 (whole body) | ⚠️ under #1 **only because** the whole body is locked — *not* a dedicated global lock | — |
| **Fastest task** I/O + journal drain + plugin cycle hooks | ✅ under #1 | ⚠️ under #1 | ✅ #3 (inside #1) |
| **Plugin via journal API** (`journal_write_*`) | ✅ drained under #1 | n/a | ✅ #3 |
| **Plugin direct I/O** (EtherCAT bus thread) | ❌ under #4 `buffer_mutex` ≠ #1 | n/a | — |
| **Debug / unix-socket thread** (force/read/write) | ❌ no lock either side | ❌ no lock either side | — |
| **`g_global_vars_mutex` #2** | — | ❌ never acquired by anyone | — |

The ✅ column for "task body" is real and sound. Every ❌ and ⚠️ is a gap.

---

## 4. The four gaps, precisely

**G1 — No real parallelism (the big lock).** Every task body runs under the single #1
mutex (`plc_state_manager.cpp:191-215`), so task bodies are mutually exclusive. The
per-task threads give independent *timing/preemption*, not throughput. Under contention,
a high-priority fast task that wakes while a low-priority task holds #1 **blocks for that
task's whole scan** (PI boosts the holder but can't erase the wait). Worst-case jitter of
the fastest task is bounded by the slowest task's body time.

**G2 — Globals have no dedicated lock; #2 is dead.** STruC++ emits globals as plain
fields and accesses them via plain reference reads/writes (`codegen.ts:2478, 2259,
2354-2356, 2556-2559`) — no guard, no atomic. `g_global_vars_mutex` is created and handed
over (`image_tables.cpp:210`) but **never locked** by anyone; `iec_threading.hpp` doesn't
exist. Globals are race-free *today only* because #1 serializes whole bodies (the ⚠️
cells). The moment #1 is narrowed for G1, globals race.

**G3 — Plugins use the wrong lock.** The EtherCAT bus thread writes the image directly
under `buffer_mutex` #4 (`plugin_driver.c:62,1004`; `ethercat_io.c:567,581`), which no
task thread holds. So plugin I/O and task bodies are **not** mutually exclusive → torn
multi-byte image words. The journal path (#3) is the *correct* one and the EtherCAT
plugin simply doesn't use it for cyclic I/O. (Note: the IEC program itself writes located
outputs *directly* too, but that's safe because it's inside #1 — journaling is only the
*plugin→image* path, not the program→image path.)

**G4 — Debug path locks nothing, on either side.** `debug_handler.c` (runtime) and
`debug_dispatch.hpp` (strucpp) both reach live `IECVar` storage with no lock
(`debug_handler.c:153,211,292`; `debug_dispatch.hpp` `force_impl/read_impl/write_impl`).
`IECVar::force()` writes `forced_`, `forced_value_`, `value_` non-atomically
(`iec_var.hpp:161-165`), so a task thread can observe a torn force-state. Plus the
ERROR/crash teardown can `dlclose` the `.so` while a debug call is mid-flight (UAF, see
companion report H3).

---

## 5. Your intended design vs. what's actually there

| You described | Reality in code |
|---|---|
| "Image tables need no lock — journaling handles writes (if you use the journal write API)." | True **only for plugins**. The IEC *program* writes located outputs **directly** (`raw_ptr()` storage), relying on #1. Plugin direct-write (EtherCAT) bypasses the journal → G3. Reads (program/debug/plugin) still need a lock; only the program gets one (#1), debug gets none → G4. |
| "Reads need locks — maybe add a thread-safe read API to image tables." | No such read API exists. Program reads are covered by #1; **debug reads have none**. A safe read API is exactly what G4 needs. |
| "Globals: one big lock, taken **only around** global access, **skipped** if the POU has no globals." | **Not implemented anywhere.** The lock object exists (#2) but is never taken; STruC++ emits no guard and has no notion of "POU has globals." This is a design that still needs to be *built into the codegen* (emit lock/unlock around global access, and omit it when a POU's `VAR_EXTERNAL` set is empty). |
| Per-task threads run in parallel. | They run on separate threads but serialize on #1 → not parallel in practice (G1). |

So the global-lock design is sound *as a plan* — it just lives only in your head and in a
vestigial mutex right now. To realize it, the **lock-around-global-access has to be
emitted by STruC++** (the runtime can't do it — it can't see individual global accesses),
and the runtime's `strucpp_set_locks` handoff finally gets a consumer.

---

## 6. Priorities (for completeness)

- STruC++ emits `TaskInstance.priority` **verbatim** from `PRIORITY := N`
  (`codegen.ts:2595`, default 0), and its own header documents **"higher = more
  important"** (`iec_std_lib.hpp:110`). The runtime maps it **directly** to a SCHED_FIFO
  level (`plc_state_manager.cpp:126-138`), higher = higher, clamped [1,99].
- **IEC 61131-3 convention is the opposite** (lower N = higher priority). Neither side
  inverts. So if users author tasks expecting IEC semantics, OS priority is **inverted**.
  This needs a deliberate decision (define the convention once, document it, invert in
  one place if required). Default `priority 0` → clamped to 1 (lowest); default
  `interval 0` → runtime treats as 20 ms.
- Even with correct priorities, G1 (the big lock) undermines them — see companion report
  §3.

---

## 7. What a correct target design needs (direction, not a prescription)

1. **Give globals their own lock and emit it in STruC++.** Either the single
   `g_global_vars_mutex` taken *only* around global read/modify/write (your original
   plan; cheap, omitted for POUs with no externals), or a small set of striped locks if
   contention matters. This is the prerequisite for everything else, and it must be
   generated by the codegen using the `strucpp_set_locks` pointers (finally wiring up the
   dead #2).
2. **Then narrow #1** so task bodies no longer serialize wholesale — e.g. lock the image
   only around the I/O copy windows (or move *all* program→image writes through a
   journal-like buffered apply, matching how plugins already work), and add the
   **thread-safe image read API** you mentioned for reads. That unlocks G1.
3. **Fix plugins to use the image lock or the journal** (G3) — make `buffer_mutex` *be*
   the image lock, or route EtherCAT cyclic I/O through the journal.
4. **Lock the debug path on the runtime side** (G4) and make `IECVar::force()` atomic or
   lock-protected; close the ERROR/crash `dlclose` UAF.

Ordering matters: **#1 (globals lock in codegen) must land before narrowing the big
lock**, or the system loses the only protection it currently has.

---

## 8. Key file references

**runtime:** `image_tables.cpp:88-90,199-210` (mutex create + dead handoff) ·
`plc_state_manager.cpp:119-235` (per-task thread, body-under-#1, priority) ·
`plc_io_cycle.cpp:22-34` (fastest-task housekeeping) ·
`journal_buffer.c` (plugin write path #3) · `plugin_driver.c:62,72-80,1004` (buffer_mutex
#4, mutex_take/give) · `debug_handler.c:135-153,203-217,265-292` (unlocked debug) ·
`runtime_v4_entry.cpp:40-57` (defines #1/#2 pointers + `strucpp_set_locks`).

**strucpp:** `src/runtime/include/iec_var.hpp:129-165,350-353` (IECVar get/set/force,
non-atomic) · `iec_located.hpp` (descriptor only, raw_ptr) ·
`iec_std_lib.hpp:107-119` (TaskInstance, "higher = more important") ·
`debug_dispatch.hpp` (unlocked force/read/write) · `src/backend/codegen.ts:2259,2354,
2478,2556-2559,2594-2599` (globals as plain refs, TaskInstance emission) ·
`src/project-model.ts:653-672,816-823` (PRIORITY/INTERVAL parsing). **No file in strucpp
contains a mutex/lock/atomic.**
