#ifndef OPENPLC_PLC_IO_CYCLE_H
#define OPENPLC_PLC_IO_CYCLE_H

#ifdef __cplusplus
extern "C" {
#endif

/*
 * I/O cycle helpers — threaded (process-image) model housekeeping.
 *
 * Encapsulates the work that has to happen once per scan around the
 * highest-priority task's body. The fastest task's thread (Phase 6 picks it;
 * ctx->is_fastest_task) calls the pre/post halves around its body; every task
 * calls the drain at its copy-in. Other task threads just run their bodies.
 *
 * See docs/strucpp-migration/07-runtime-v4-plugin-and-io.md for the
 * topology rationale.
 *
 *   plc_run_io_cycle_threaded_drain() — apply pending journal entries to the
 *     image. Called at EVERY task's copy-in, under the image mutex, so each
 *     task sees freshly-applied plugin/peer writes.
 *
 *   plc_run_io_cycle_threaded_pre()  — fire plugin cycle_start (fastest task,
 *     before bodies; no image lock held).
 *
 *   plc_run_io_cycle_threaded_post() — advance time, fire plugin cycle_end,
 *     update heartbeat, increment scan_counter (fastest task, after bodies).
 */
void plc_run_io_cycle_threaded_drain(void);
void plc_run_io_cycle_threaded_pre(void);
void plc_run_io_cycle_threaded_post(void);

#ifdef __cplusplus
}
#endif

#endif /* OPENPLC_PLC_IO_CYCLE_H */
