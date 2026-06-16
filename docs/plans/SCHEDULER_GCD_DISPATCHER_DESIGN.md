# GCD Master-Tick Dispatcher — Scheduler Redesign

Status: **proposal / for review** — no code yet.
Scope: `core/src/plc_app/plc_state_manager.cpp`, `plc_io_cycle.{c,h}`,
`image_tables.cpp`, `utils/watchdog.c`, and a small strucpp-runtime touch
(time snapshot). Builds on the per-task exception isolation already landed on
`feat/threaded-only-and-per-thread-faults`.

---

## 1. Why the current scheduler is wrong

Today (`plc_task_thread`) every IEC task is a free-running SCHED_FIFO pthread
that sleeps on its own `clock_nanosleep(TIMER_ABSTIME)` at its own interval,
and once-per-scan housekeeping (IEC time advance, `cycle_start`/`cycle_end`,
heartbeat, `scan_counter`) is pinned to whichever task is *fastest*
(`is_fastest_task`). Three defects fall out of that:

1. **Wrong time quantum.** The anchor advances `__CURRENT_TIME_NS` by
   `base_tick_ns` (the GCD of all intervals) but only runs at its *own*
   interval (the minimum). GCD ≤ min, so IEC time runs **slow** whenever
   intervals aren't harmonic (e.g. 19 ms + 20 ms → GCD 1 ms, anchor runs every
   19 ms → clock at ~1/19 real rate).

2. **Wrong time grid even if the quantum were fixed.** Anchoring on a *real*
   task means `TIME()` only moves at that task's execution instants — a lattice
   of the anchor's period. A different task's timer can never observe its own
   period exactly. Only the GCD divides *every* task's period, so only a
   GCD-spaced clock lets every task see an integer tick count at its own scan
   boundary.

3. **No common phase / blind watchdog.** Each task calls `clock_gettime()` at
   its own thread start, so task grids aren't even phase-aligned with each
   other. And the watchdog only sees the global `plc_heartbeat`, fed by the
   anchor alone — a stalled *slow* task is invisible; a stalled *fastest* task
   takes down the whole PLC.

The fix is to stop anchoring on a real task and introduce a **single time base
at the GCD** that *drives* the workers, rather than running beside them.

---

## 2. Goals / non-goals

**Goals**
- IEC `TIME()` advances at real rate on a grid (GCD) that aligns with every
  task's period — deterministic `TON`/`TOF`/`TP` regardless of interval mix.
- One coherent clock and one phase origin (`t0`) for all tasks.
- Keep task bodies running **in parallel, lock-free** on private storage (the
  win of the threaded model — do not regress to a global body lock).
- Per-task fault isolation (already implemented) integrates cleanly: a faulted
  task is dropped from the schedule; the rest keep running.
- Per-task watchdog keyed to each task's own deadline.

**Non-goals**
- Hard real-time guarantees beyond what SCHED_FIFO + a tuned kernel give.
- Changing the plugin journal/image contract (it already works; we lean on it).
- Sub-microsecond base ticks (see §4 floor policy).

---

## 3. Architecture: one dispatcher + N workers

```
            ┌──────────────────────────────────────────────┐
            │  master-tick dispatcher  (was: bootstrap)     │
            │  clock_nanosleep(TIMER_ABSTIME) @ base_tick    │
            │  anchored on a single t0                       │
            │                                                │
            │  every tick N:                                 │
            │    __CURRENT_TIME_NS += base_tick   (always)   │
            │    plc_heartbeat = now              (always)   │
            │    due = { task : N % task.divisor == 0 }      │
            │    if due not empty:                           │
            │        journal drain (image_lock/unlock)       │
            │        cycle_end()   (prev frame committed)*   │
            │        cycle_start() (new frame opening)       │
            │        stamp time + release due & alive tasks  │
            │        scan_counter++                          │
            │        (NO wait — see §5)                      │
            │    * cycle_end skipped on the first frame      │
            └───────────────┬────────────────────────────────┘
                            │ release = sem_post / cond signal
              ┌─────────────┼─────────────┬───────────────┐
              ▼             ▼             ▼               ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐    ┌──────────┐
        │ worker 0 │  │ worker 1 │  │ worker 2 │ …  │ worker k │
        │ SCHED_FIFO  │ SCHED_FIFO  │ SCHED_FIFO     │ SCHED_FIFO
        │ wait(go) →  │ copy_in →   │ sync_in →      │ run() →
        │ sync/run/   │ sync_out →  │ copy_out →     │ wait(go)…
        │ commit loop │ wait(go)…   │ wait(go)…      │
        └──────────┘  └──────────┘  └──────────┘    └──────────┘
```

`divisor = task.interval_ns / base_tick_ns` (an integer because base_tick is
the GCD). On tick `N`, a task is due iff `N % divisor == 0`. Computed once at
load; the per-tick test is a handful of integer mods.

### Three layers, kept distinct

- **L0 — Time base.** The dispatcher bumps `__CURRENT_TIME_NS` by `base_tick`
  every tick. This is the *only* writer.
- **L1 — Plugin ↔ process image.** Journal (lock-free, plugin→image) + image
  drain on lock (applied at each worker's copy-in) + optional `cycle_start`/
  `cycle_end` notifications. **Well-behaved plugins self-pace their bus I/O on
  their own thread** (see §7) — EtherCAT already does, so L1 is mostly the
  journal, not the cycle hooks.
- **L2 — Process image ↔ task-private storage.** Per-task `copy_in`/`sync_in`
  before the body and `sync_out`/`copy_out` after — unchanged from today, this
  is what makes parallel lock-free bodies safe.

The dispatcher reuses the existing bootstrap thread (`plc_cycle_thread`): today
it spawns workers then polls state every 1 s; it becomes the tick loop instead.
It already owns SIGFPE/SIGSEGV crash recovery.

---

## 4. The master tick (base period)

- `base_tick_ns = GCD(all task intervals)` — already computed
  (`compute_base_tick_from_config`).
- Single `t0`: the dispatcher samples `CLOCK_MONOTONIC` once, then every
  wake-up is `t0 + N·base_tick` via `TIMER_ABSTIME` (no drift accumulation).
- **Floor / warning policy** (per review): whole-millisecond intervals always
  yield GCD ≥ 1 ms, so the normal case is fine. If `base_tick_ns < 1 ms`
  (someone used fractional/sub-ms intervals that are near-coprime), **log a
  warning** — "base tick = X µs → housekeeping at Y kHz; consider harmonizing
  task intervals." Do **not** clamp the tick up: clamping reintroduces grid
  misalignment (the sub-ms task would stop landing on tick boundaries). Run at
  the true GCD; if it's physically too fast to service, the existing
  scan-overrun detection (§9) reports it. (Optional: a hard *reject* below an
  absolute floor like 100 µs, where servicing is hopeless — separate policy.)

---

## 5. How workers are "called" — synchronization & the no-wait rule

> *Direct answer to: "How are these threads called? Sleep on a mutex? What's
> the wake overhead vs pure time sleep?"*

### Mechanism: one release primitive per worker

Each worker no longer sleeps on a timer; it blocks waiting to be released. The
recommended primitive is a **per-worker binary release** — either a POSIX
semaphore (`sem_t`, one per worker) or a mutex+condvar pair. Recommendation:
**semaphore**, because:
- `sem_post` from the dispatcher needs no lock held and has no spurious-wakeup
  retry loop;
- it carries a release/acquire memory barrier (so the worker is guaranteed to
  observe the time bump the dispatcher did *before* posting — see §6);
- "binary" (cap the pending count at 1) so a missed activation can't queue and
  death-spiral (see overrun, §9).

Worker loop:

```c
while (running) {
    sem_wait(&ctx->go);                 // blocks until released (or shutdown)
    if (!running) break;
    snapshot_time_into_private(ctx);    // §6
    image_lock(); copy_in(ctx); image_unlock();   // L2 in (drains journal)
    lock(global); sync_in(ctx); unlock(global);
    try { run_bodies(ctx); }            // §8: catch → mark dead, return
    catch (...) { mark_dead(ctx); log; return; }
    lock(global); sync_out(ctx); unlock(global);
    copy_out(ctx);                      // journal (lock-free)
    ctx->completed.fetch_add(1);        // watchdog liveness (§9)
}
```

### The no-wait rule (critical for multi-rate correctness)

The dispatcher **releases due workers and immediately returns to sleep until
the next tick. It does NOT wait for workers to finish.** Reason: a long-period
task's body may legitimately span many base ticks (a 100 ms task body taking
5 ms while base_tick is 1 ms). If the dispatcher blocked on completion, the
time base would stall for the duration of the slowest body — fatal. So the time
bump and the next tick proceed unconditionally; worker bodies run
asynchronously relative to the tick.

Consequence: there is **no dispatcher-side barrier and no `cycle_end` that waits
on the bodies.** Output commit is deferred through the journal — a worker
journals its outputs at `copy_out` (lock-free), and they apply to the image on
the next drain. This is a standard one-base-tick output latency (≤ 1 ms at a
1 ms tick): deterministic and negligible. (See §7 for the `cycle_end` mapping.)

What the dispatcher's release *does* buy us, vs free-running workers, is the
two correctness fixes: workers fire exactly on their period boundary aligned to
the single `t0`, and they observe the time bump that happened-before the
release. We get phase alignment and a race-free clock read **without** paying a
completion barrier.

### Overhead vs `clock_nanosleep`

| | Free-running (today) | Dispatcher (proposed) |
|---|---|---|
| Wake path | kernel timer IRQ → worker | kernel timer IRQ → dispatcher → futex wake → worker |
| Timers armed | N (one per task) | 1 (the dispatcher) |
| Per-release cost | — | one `sem_post` (futex wake): ~1–3 µs when a waiter is present |
| Extra latency to body start | 0 | one context switch: dispatcher→worker, ~1–5 µs on SCHED_FIFO |

A `sem_post`/futex wake and a timer wake-up are the same *order* of cost — both
end in the scheduler dispatching a runnable thread. The dispatcher model adds
**one extra scheduling hop** (timer→dispatcher→worker instead of timer→worker
directly): roughly **a few microseconds** of added latency between the tick
boundary and a worker starting its body, plus the dispatcher's per-tick bookkeeping
(integer mods + a time add — sub-µs).

At a 1 ms base tick that's < 0.5 % jitter, and it's *deterministic* (always the
same hop), which matters more than the raw number for IEC timing. Net trade: we
swap N independent timers for 1 timer + N cheap futex wakes, and we pay one
context-switch of latency to gain a coherent, race-free, phase-aligned clock.

**Mitigations** (mostly already-plumbed TODOs):
- Pin the dispatcher and workers to isolated CPUs (`CPU_AFFINITY`, already a
  Phase-8 stub) so the wake doesn't cross-migrate.
- Keep the dispatcher's pre-release work minimal (precomputed divisors).
- Keep the dispatcher at a priority ≥ the highest worker so its tick is never
  preempted by a worker mid-release.

### Rejected alternative

*Keep workers free-running and derive time from `CLOCK_MONOTONIC`
(thread-local `__CURRENT_TIME_NS`).* Lowest wake overhead (no extra hop), but
abandons the single coherent clock and the common phase origin (each worker's
`t0` differs). Fine for `TON` deltas; loses cross-task `TIME()` consistency.
Keep as fallback if the one context-switch hop ever proves intolerable.

---

## 6. Time semantics — dispatcher stamps the snapshot at dispatch

The generated program reads IEC `TIME()` from a single global
`__CURRENT_TIME_NS` in the `.so`. With parallel multi-rate tasks a single shared
global cannot give each task a scan-stable time (a value set for task A is
overwritten before/while task B reads it). The fix has two halves:

1. **`__CURRENT_TIME_NS` is `thread_local`** under `STRUCPP_THREADED`
   (`iec_std_lib.hpp`) so every worker thread has its own IEC clock. It stays a
   plain global on single-threaded targets (Arduino), which have no TLS runtime
   and don't need it. The shim exports `strucpp_set_current_time(int64_t)`,
   which sets the **calling thread's** instance.
2. **The dispatcher stamps; the worker applies:**

```c
// dispatcher, tick N, each due+alive task that is idle:
ctx->time_at_dispatch = master_time;        // == N * base_tick
sem_post(&ctx->go);
// worker on wake — runs ON the worker thread so the thread_local lands right:
ext_strucpp_set_current_time(ctx->time_at_dispatch);
run_body();
```

Result:
- Each task's `TIME()` is **constant for its whole scan** and equals the time at
  which the dispatcher released it — correct 61131-3 semantics.
- A **slow/overrunning** worker keeps reading its own thread_local snapshot
  until it finishes; the dispatcher's master clock advances freely for the other
  tasks. No coupling, no cross-thread race on the global.
- The master clock (`master_time = tick · base`) lives only in the dispatcher;
  it never touches the `.so` global directly.

This needs a small strucpp change — the `thread_local` qualifier plus the
`strucpp_set_current_time` shim. `strucpp_advance_time` is retained for
single-threaded hosts but unused by the dispatcher.

---

## 7. Plugin I/O frame & the idle-tick fast path

### Idle-tick fast path

On a tick with **no** task due (only happens in non-harmonic configs; in a
harmonic config the fastest task is due every tick), the dispatcher does
**only** the time bump + heartbeat and goes back to sleep. It skips the journal
drain, `cycle_start`/`cycle_end`, and `scan_counter` — nothing reads the image
that tick, so there is nothing to sync.

- `scan_counter`: bumped on **task-bearing ticks only** (a "scan" should mean
  logic ran — that's what the debugger's cycle-boundary detection wants).
- `plc_heartbeat`: bumped **every** tick (on the always-run path) so the global
  watchdog is robust regardless of idle runs.

### Why skipping the cycle hooks is safe — the plugin contract

We verified this against the EtherCAT plugin (the heaviest native plugin):

- EtherCAT runs a **dedicated SCHED_FIFO bus thread** (`ecat_bus_thread`) at its
  own configured `cycle_time_us` (default 1 ms). That thread does the entire
  output→wire→input cycle (`ecat_run_one_cycle`): snapshot %Q into the IOmap
  under `image_lock`, exchange process data with slaves, publish %I back through
  the **journal** (lock-free). Slave sync-manager watchdogs and DC are fed by
  *this* thread, independent of the PLC scan.
- So EtherCAT's I/O does **not** depend on the PLC `cycle_start`/`cycle_end`
  hooks at all; the journal is the integration point.

This yields the contract we standardize on:

> **Plugins own their bus timing.** Any cyclic/isochronous wire I/O (and the
> slave watchdogs that depend on it) runs on the plugin's own thread at its own
> rate, bridging to the process image via the journal (writes) and `image_lock`
> (output snapshot). `cycle_start`/`cycle_end` are PLC-image⟷plugin **sync
> notifications**, invoked only when PLC logic actually runs — they are **not**
> wire-frame triggers.

Under this contract the idle-tick skip needs **no per-plugin opt-out**: a
plugin that needs frames at a fixed rate runs its own thread (the correct
pattern). We deliberately do *not* add a `requires_cyclic_frame` flag, because
it would invite the anti-pattern (synchronous wire I/O inside `cycle_start`
with no thread). If a future plugin can't self-pace, the fix is to give it a
thread, not to make the scheduler frame it.

### `cycle_start` / `cycle_end` mapping

In a multi-rate threaded model **there is no single "end of cycle"** — each task
is its own cycle with its own boundary, and the dispatcher must not wait for
bodies (§5; waiting penalizes every task for the slowest one). The key insight
is that we don't *need* a scan-end barrier: `cycle_end` historically existed to
commit outputs after the logic ran, and **the journal already does that,
continuously and lock-free** — each worker journals its outputs at `copy_out`
the instant its body ends, and self-pacing plugins (EtherCAT's bus thread) drain
and push on their own cadence. The output **data path** therefore needs no
global barrier; outputs commit when each task produces them.

That collapses `cycle_end` from a synchronization point to a notification.
Both hooks stay **global** and are fired back-to-back by the dispatcher at the
top of every task-bearing tick, in this order:

```
task-bearing tick:
    drain journal            # apply prev frame's journaled %Q (+ plugin %I) to image
    cycle_end()              # "the previous frame's outputs are now drained/committed"
    cycle_start()            # "a new frame is beginning"
    stamp time + release due&alive workers
    scan_counter++
```

So **`cycle_end` is fired at the *next* tick's frame top, right after the drain
that commits the previous frame's outputs** — its contract is precisely "the
previous frame's outputs are now drained." This is exact (the drain really
happened), needs no barrier, and costs no extra timer. `cycle_start` then opens
the new frame. The very first task-bearing tick has no prior frame, so
`cycle_end` is skipped on tick 0 (a `frame_started` latch gates it).

Rejected alternatives:
- **Waiting for all bodies** before `cycle_end` — penalizes every task for the
  slowest one; also impossible without stalling the time base (§5).
- **A `GCD/2` timer** — doesn't *guarantee* bodies finished (a body legitimately
  > GCD/2 fires it mid-scan) and adds a second timer wake every tick to
  approximate what the journal already gives us exactly.

Plugins must not assume `cycle_end` means "every task body this scan has
finished" — it means "the previous frame's outputs are committed to the image."
That weaker (but exact) contract is all a multi-rate threaded model can
honestly offer, and it's what self-pacing plugins actually need. The dispatcher
thus only ever sleeps, bumps time, drains, fires the two global hooks, and
releases: it never blocks on a body and never arms a second timer.

---

## 8. Exception handling integration

> *Direct answer to: "What happens if a thread throws? Will the master tick call
> an exceptioned thread?"*

The per-task exception isolation already implemented (on
`feat/threaded-only-and-per-thread-faults`): the worker wraps its scan body in
`try/catch`; on an unrecoverable STruC++ fault (which `throw`s on this
exceptions-enabled hosted build) it releases any held lock, logs, and
**terminates its own thread** (`return`), leaving the others running. We extend
that with one field so the dispatcher cooperates:

- **`ctx->alive` (atomic bool), default true.** In the worker's `catch`, set
  `ctx->alive = false` **before** returning.
- **The dispatcher gates release on `alive`:** `for (t in due) if (t.alive)
  release(t);`. So the dispatcher **never posts `go` to a terminated worker** —
  a faulted task is simply skipped on every subsequent tick, i.e. removed from
  the schedule. No `sem_post` to a dead thread, no join-deadlock, no leak.

Concretely, answering the questions:

- *Will the master tick call an exceptioned thread?* **No.** Once a worker marks
  itself dead, the dispatcher's `alive` check excludes it from every future
  release. The dispatcher keeps ticking and keeps releasing the *surviving*
  tasks. The faulted task's outputs simply freeze (it stops `copy_out`).
- *Does the PLC stop?* No — it stays RUNNING minus the faulted task (unless the
  faulted task was the housekeeping anchor under the *old* model; in the new
  model housekeeping is on the dispatcher, **not** on any worker, so a worker
  fault never affects the time base or heartbeat. This is a real improvement the
  dispatcher gives us for free).
- *Shutdown of a dead worker.* It already returned, so it's joinable
  immediately; teardown (§11) joins all worker handles including dead ones
  (`pthread_join` on a returned thread is fine).

Difference vs a *stuck* (not throwing) worker — infinite loop in the body: a
`sem_post` can't break that; it's the watchdog's job (§9), not the exception
path's.

---

## 9. Watchdog redesign — per-task, dispatcher-driven

The global single-heartbeat watchdog is replaced by **per-task deadline
monitoring**, naturally hosted on the dispatcher (which already iterates tasks
each tick and knows due/alive/completion state):

- Per task, track `released` vs `completed` (atomic counters; worker bumps
  `completed` at end of scan, dispatcher bumps `released` on `sem_post`).
- **Overrun:** if the dispatcher is about to release a task whose
  `released > completed` (it never finished the previous activation), that's a
  scan overrun for that task. Because the release is *binary* (§5), the missed
  activation does not queue.
- **Deadline:** a task is unhealthy if `released - completed` stays ≥ 1 for
  longer than `K · interval_ns` (K configurable, e.g. 2–3).
- **Policy on overrun/stall** (per task, configurable):
  - *flag* — log + expose in diagnostics, keep releasing (transient overrun);
  - *drop* — mark `alive = false` (same mechanism as a fault), keep the rest
    running;
  - *escalate* — `plc_force_error_state()` for safety-critical configs.
- The global `plc_heartbeat` stays as a coarse "dispatcher is alive" signal
  (bumped every tick); if the *dispatcher* stalls, the existing watchdog still
  catches it.

This is where the dispatcher model pays off again: housekeeping and liveness
monitoring live on one thread that is, by construction, the time authority.

---

## 10. Shutdown & state transitions

- On STOP/ERROR, the dispatcher flips `plc_state`, then **posts every worker's
  `go` once** (with `running=false` observed) so each wakes from `sem_wait`,
  sees the state, and returns. This replaces today's
  `pthread_kill(SIGUSR1)`-to-break-`clock_nanosleep` dance (workers no longer
  sleep in `clock_nanosleep`).
- A worker stuck in a body (not at `sem_wait`) won't observe the post; SIGUSR1
  remains as the escalation to interrupt a worker blocked in a syscall, and the
  watchdog's *escalate* policy covers a pure CPU-bound infinite loop.
- Join all workers, free per-task contexts under `plc_tasks_lock` (unchanged
  teardown discipline), destroy semaphores.

---

## 11. Implementation phases

1. **Dispatcher skeleton.** Convert `plc_cycle_thread` from poll-loop to GCD
   tick loop (single `t0`, `TIMER_ABSTIME`). Keep workers free-running for now;
   dispatcher only owns time + heartbeat + scan_counter. Validates the time
   base in isolation. (Fixes defect #1/#2 immediately.)
2. **Release plumbing.** Add per-worker semaphore + `alive`/`released`/
   `completed`. Switch workers from `clock_nanosleep` to `sem_wait`; dispatcher
   releases due+alive workers (no-wait). Time snapshot at release (§6).
3. **Idle-tick fast path + cycle-hook mapping** (§7), with the plugin-contract
   doc change.
4. **Per-task watchdog** (§9); retire the anchor concept.
5. **Strucpp touch (small).** `__CURRENT_TIME_NS` becomes `thread_local` under
   `STRUCPP_THREADED` and the shim gains `strucpp_set_current_time()` (§6); the
   worker calls it at dispatch. Plain global on single-threaded targets.

Each phase is independently testable; #1 alone is shippable and fixes the timing
bug even before the dispatcher drives workers.

---

## 12. Build / test flow

The runtime is validated end-to-end via its installer, **not** by compiling
objects by hand:

```
sudo systemctl stop openplc-runtime     # stop the running service
sudo ./install.sh                        # rebuild + reinstall the daemon
sudo systemctl start openplc-runtime     # (installer may start it itself)
```

Functional validation uploads multi-task projects (via the OpenPLC Runtime v4
CLI utility) and inspects the runtime log for tick timing, per-task scan
period/jitter, and overrun behavior when a task body exceeds its interval.

---

## 13. Open questions

- Hard-reject threshold for sub-µs base ticks, or warn-only? (Current: warn.)
- Should the *drop* watchdog policy be the default (graceful) or *escalate*
  (safe)? Likely per-deployment config with a safe default. (Current: *flag*.)
- Worker priority vs dispatcher priority ordering on a non-isolated CPU — verify
  the dispatcher tick can't be starved by a busy worker at equal priority.
  (Current: dispatcher runs at SCHED_FIFO 99, above workers.)
