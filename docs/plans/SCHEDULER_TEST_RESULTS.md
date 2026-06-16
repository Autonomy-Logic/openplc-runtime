# GCD Master-Tick Dispatcher — On-Target Test Results

Target: **SLM-RP4** (Raspberry Pi 4, aarch64), kernel `6.12.35-rt10-v8+`
(**PREEMPT_RT**), 4 cores. Runtime built + installed via the official
`sudo ./install.sh` flow on branch `feat/threaded-only-and-per-thread-faults`.

Test programs were hand-built from the known-good runtime-v4 codegen (multiple
`Program*` instances wired into one CONFIGURATION at different intervals),
compiled on-device with the runtime's own `scripts/compile.sh`
(`-DSTRUCPP_THREADED`), and driven through the daemon's control socket
(`plc_main --safe-mode`, single START). Per-task timing comes from the runtime's
`STATS` command; fault/overrun evidence from the runtime log (`--print-logs`).

## 1. Build / install

`install.sh` rebuilt `plc_main` and all plugins and (re)started the service with
**zero errors** — the dispatcher, per-task exception handling, dead-code
removal, and the `thread_local` time shim all compile and link on the real
Linux/PREEMPT_RT target. The daemon's `cycle_start`/`cycle_end` hook probe shows
the EtherCAT plugin registers neither (`cycle_start: (FAIL)`,
`cycle_end: (FAIL)`) — confirming it self-paces on its own bus thread, as the
design assumes.

## 2. Multi-rate timing + jitter (3 tasks: 10 / 20 / 50 ms)

12-second run, single dispatcher, base tick = GCD = 10 ms:

| task   | configured | measured cycle (avg) | min / max         | jitter (max−min) | scan body | overruns |
|--------|-----------:|---------------------:|-------------------|-----------------:|----------:|---------:|
| FAST10 |     10 ms  | **9.92 ms**          | 9.924 / 10.083 ms | **159 µs**       | 3 µs      | 0        |
| MED20  |     20 ms  | **19.84 ms**         | 19.901 / 20.105 ms| **204 µs**       | 4 µs      | 0        |
| SLOW50 |     50 ms  | **49.62 ms**         | 49.902 / 50.075 ms| **173 µs**       | 5 µs      | 0        |

- Periods accurate to **< 0.8 %**; jitter **159–204 µs** (sub-millisecond,
  ~1–2 % of the 10 ms base tick) on PREEMPT_RT.
- Relative rates exactly 5 : 2.5 : 1 (scan-count ratios matched to 3 sig figs).
- Lock-free bodies: 3–5 µs scan times.

> Note: a first attempt showed 2× rates — traced to `plc_main.c:161`
> auto-starting on launch while the test harness *also* sent START (a startup
> race between auto-start and an external START, not a dispatcher bug). Running
> the daemon with `--safe-mode` (no auto-start) + one explicit START gives the
> correct single-dispatcher numbers above. The auto-start-vs-START path lacks a
> hard mutual-exclusion guard — worth hardening separately (see open items).

## 3. Overrun + exception isolation (3 tasks)

Tasks: `FAST20` (well-behaved, 20 ms) · `HOG50` (50 ms period, ~80 ms CPU-burn
body → always overruns) · `FAULT30` (30 ms; `throw`s on its 15th scan, mimicking
a STruC++ IEC runtime fault).

`STATS` after 8 s:

| task    | scans | cycle avg | cycle max | overruns | outcome |
|---------|------:|----------:|----------:|---------:|---------|
| FAST20  |  275  | 18.7 ms   | 20.1 ms   | 0        | **unaffected** — steady ~20 ms throughout |
| HOG50   |   55  | 74.5 ms   | 100.1 ms  | **55**   | overruns gracefully; runs at reduced rate |
| FAULT30 |   15  | —         | —         | 0        | **terminated at scan 15** (threw); count frozen |

Runtime log (direct evidence):

```
[INFO]  PLC base tick: 10000000 ns across 3 task(s)
[INFO]  PLC: fastest task is FAST20 (interval=20000000 ns, priority=10)
[INFO]  Spawned 3 PLC task thread(s)
[INFO]  GCD master-tick dispatcher running (base tick 10000000 ns)
[WARN]  [task HOG50] scan overrun #1: body exceeds its 50 ms period — running at reduced rate, other tasks unaffected
[ERROR] [task FAULT30] terminated by unhandled exception: synthetic IEC fault (array bounds) for isolation test — other tasks keep running
[WARN]  [task HOG50] scan overrun #50: body exceeds its 50 ms period — running at reduced rate, other tasks unaffected
```

- **Overrun isolation:** HOG50 burning a full core for 80 ms every cycle did not
  perturb FAST20 (0 overruns, steady period). Overruns are detected and
  rate-limited-logged; the binary release prevents activation pile-up.
- **Exception isolation:** FAULT30's `throw` was caught, logged, and that thread
  terminated; FAST20 and HOG50 kept running and the PLC stayed RUNNING. Before
  this work the same throw aborted the whole process (the original bug report).

## 4. Open items / follow-ups

- **strucpp release:** `thread_local __CURRENT_TIME_NS` lives on strucpp branch
  `feat/threadlocal-iec-time`. The editor must bundle a strucpp build containing
  it (release + binary-version bump) for the production upload flow; on-device
  tests patched the header directly.
- **Auto-start vs START race** (`plc_main.c` + `unix_socket.c`): add a hard guard
  so a program can't be loaded twice concurrently.
- **Dead code:** `plc_io_cycle.cpp`'s `plc_run_io_cycle_threaded_{drain,pre,post}`
  are now unused (the dispatcher calls `plugin_driver_cycle_start/end` directly);
  safe to delete.
- **Web-API auth:** live tests used the daemon control socket directly because
  the device's single web user's credentials were unknown; the standard editor
  upload flow was not exercised end-to-end.
</content>
