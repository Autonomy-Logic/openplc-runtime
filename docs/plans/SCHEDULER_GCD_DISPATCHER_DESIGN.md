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
            │        cycle_start  (optional plugin hook)     │
            │        for t in due & t.alive: release(t)      │
            │        scan_counter++                          │
            │        (NO wait — see §5)                      │
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

## 6. Time semantics — snapshot at release

`__CURRENT_TIME_NS` is a single `inline int64_t` in the `.so` (one instance for
all threads). The dispatcher is the sole writer; it bumps it *before* posting a
worker's `go`. Because `sem_post`/`sem_wait` establish happens-before, a worker
released this tick sees the freshly bumped value.

But a **slow** worker from a previous tick may still be running when the
dispatcher bumps the next tick — a concurrent read of the non-atomic global by
that worker while the dispatcher writes is a data race. Two clean fixes; we take
the second:

1. Make the time global `std::atomic<int64_t>` — a small strucpp-runtime change.
2. **Snapshot the master time into the program's private time slot at the top of
   the worker's scan** (right after `sem_wait`, before the body). The body reads
   the snapshot, not the live global. This (a) removes the race entirely and
   (b) gives correct IEC semantics — `TIME()` is *constant within a scan*, which
   is what 61131-3 expects anyway. This is the recommended approach.

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

`cycle_start` fires at the top of a task-bearing tick (after the drain, before
releasing workers) as an optional "scan starting" notification. Because the
dispatcher does not wait for bodies (§5), there is no post-body barrier on which
to hang a meaningful `cycle_end`; output commit is journal-deferred. Options to
settle during implementation:

- (a) Fire `cycle_end` on the *next* task-bearing tick's drain (fold it into the
  "frame" at the top of the tick) — outputs have one-tick latency, no barrier.
- (b) Keep `cycle_end` as a pure notification fired immediately after releasing
  workers (semantically "scan dispatched"), documented as non-blocking.

Recommendation: (a), and document that plugins must not assume `cycle_end`
means "all task bodies for this scan have finished" — that assumption never held
across multi-rate tasks anyway.

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
5. **Strucpp touch** if we choose atomic time instead of snapshot (we prefer
   snapshot → no strucpp change needed).

Each phase is independently testable; #1 alone is shippable and fixes the timing
bug even before the dispatcher drives workers.

---

## 12. Open questions

- `cycle_end` mapping (a) vs (b) in §7 — pick during phase 3 with a plugin pass.
- Hard-reject threshold for sub-µs base ticks, or warn-only?
- Should the *drop* watchdog policy be the default (graceful) or *escalate*
  (safe)? Likely per-deployment config with a safe default.
- Worker priority vs dispatcher priority ordering on a non-isolated CPU — verify
  the dispatcher tick can't be starved by a busy worker at equal priority.
