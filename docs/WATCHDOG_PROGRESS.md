# Watchdog progress tracking

Both the dispatcher and the legacy threaded I/O post-hook feed the global
watchdog through `watchdog_feed()`. Its unsigned atomic counter advances
independently of CLOCK_REALTIME. The reader compares progress across its existing
two-second sampling interval; system-time steps cannot impersonate a stalled
producer. Non-running and transitioning states refresh the reader baseline
without resetting a concurrently advancing producer. Existing stuck-transition
timeout behavior is retained. Unsigned rollover is supported by equality tests.

This global watchdog observes dispatcher progress, not completion of every IEC
task. It does not change per-task timing/statistics, the sampling interval, or
the existing limitations for scheduling periods longer than that interval.
Run `bash tests/host/run.sh` for deterministic tests of the production watchdog
with injected sleep/state boundaries; no system clock or real device is changed.
