# SPDX-License-Identifier: MIT

"""Execute the production dispatcher wait loop with deterministic completions.

This is a focused host regression, not a full PLC/runtime integration test.
The loop is extracted verbatim so deleting the production fix breaks the test.
Run: python3 tests/host/test_dispatch_debug_drain.py
"""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'core/src/plc_app/plc_state_manager.cpp').read_text()
start = source.index('        pthread_mutex_lock(&done_mutex);\n        for (;;)')
end = source.index('        pthread_mutex_unlock(&done_mutex);\n    }', start)
loop = source[start:end] + '        pthread_mutex_unlock(&done_mutex);\n'
prefix = r"""
#include <atomic>
#include <cassert>
#include <cerrno>
#include <cstdio>
struct timespec { long tv_sec, tv_nsec; };
int done_mutex, done_cond;
int lock_depth, image_depth, drains, hooks, copies, saves, waits;
bool pending, running = true, complete_on_wait;
std::atomic<int> g_tasks_running{0};
void *plugin_driver = reinterpret_cast<void *>(1);
enum { PLC_STATE_RUNNING, PLC_STATE_STOPPED };
int plc_get_state() { return running ? PLC_STATE_RUNNING : PLC_STATE_STOPPED; }
int pthread_mutex_lock(int *) { assert(lock_depth == 0); ++lock_depth; return 0; }
int pthread_mutex_unlock(int *) { assert(lock_depth == 1); --lock_depth; return 0; }
void image_lock() { assert(lock_depth == 0 && image_depth == 0); ++image_depth; }
void image_unlock() { assert(image_depth == 1); --image_depth; }
void image_tables_copy_config_globals_out() { ++copies; }
void debug_write_journal_drain() {
    assert(image_depth == 1 && g_tasks_running.load() == 0);
    if (pending) { ++drains; pending = false; }
}
void plc_retain_save() { ++saves; }
void plugin_driver_cycle_end(void *) { ++hooks; }
int pthread_cond_timedwait(int *, int *, timespec *) {
    assert(lock_depth == 1);
    if (++waits == 1 && complete_on_wait) { g_tasks_running.store(0); return 0; }
    return ETIMEDOUT;
}
void run(bool cycle_end_pending) {
    timespec next_tick{};
"""
suffix = r"""
}
void reset(int active, bool completion) {
    lock_depth = image_depth = drains = hooks = copies = saves = waits = 0;
    pending = running = true; g_tasks_running.store(active); complete_on_wait = completion;
}
int main() {
    reset(1, true); run(false); // retired overrun, then worker completion
    assert(drains == 1 && !pending && hooks == 0 && copies == 0 && saves == 0);
    reset(1, false); run(false); // still active: never mutate
    assert(drains == 0 && pending);
    reset(0, false); run(true); // normal frame: hooks run once
    assert(drains == 1 && hooks == 1 && copies == 1 && saves == 1);
    reset(0, false); running = false; run(false);
    assert(drains == 0 && pending);
    puts("PASS: late completion, active-worker exclusion, normal hooks, STOP");
}
"""
with tempfile.TemporaryDirectory(prefix='openplc-dispatch-test-') as directory:
    src = Path(directory) / 'test.cpp'
    src.write_text(prefix + loop + suffix)
    exe = Path(directory) / 'test'
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++17', '-Wall', '-Wextra',
                    '-Werror', str(src), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
