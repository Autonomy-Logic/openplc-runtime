# Queued writes after an overrun

External debug writes and forces can be drained whenever all task workers have
finished and the dispatcher holds the process-image lock. A previous overrun
may already have retired cycle_end at the next tick without releasing another
task. Its later worker completion still creates a valid mutation window.

The additional idle-window drain does not repeat plugin cycle_end, RETAIN save,
or output copy-out. It does not allow mutation while a worker is active. Protocol
and queue capacity are unchanged. Run `bash tests/host/run.sh` for the focused
regression, which executes the actual wait-loop source with deterministic worker
completion and mocked image/plugin hooks. Full PLC timing validation is separate.
