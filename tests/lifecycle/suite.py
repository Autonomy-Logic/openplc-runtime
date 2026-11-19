# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""PR #162 review findings: one test per finding, run against a real runtime.

Each test drives plc_main directly over its command socket, with the fake VPP
plugin providing a slow bring-up (so a transition is wide enough to aim at) and a
file-driven mode switch. Oracles are chosen to be observable from outside the
process -- socket replies, the journal, and /proc/<pid>/maps -- so the same suite
scores the pristine branch and the fixed one.
"""

import ctypes
import os
import re
import signal
import socket
import subprocess
import sys
import time

SOCK = "/run/runtime/plc_runtime.socket"
SWITCH = "/tmp/fakevpp_switch"
ARM = "/tmp/failinject_arm"
results = []


# ---------------------------------------------------------------- plumbing
def cmd(c, timeout=20):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    s.sendall((c + "\n").encode())
    r = s.recv(4096).decode().strip()
    s.close()
    return r


def status():
    try:
        return cmd("STATUS")
    except OSError:
        return "unreachable"


def wait_final(limit=120.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        st = status()
        if "TRANSITIONING" not in st and st != "unreachable":
            return st, time.time() - t0
        time.sleep(0.02)
    return status(), time.time() - t0


def wait_transitioning(limit=20.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        if "TRANSITIONING" in status():
            return True
        time.sleep(0.005)
    return False


def ensure_running(limit=25.0):
    """Get the PLC to RUNNING, tolerating the boot-auto-start race.

    plc_main creates the command socket before it claims the boot start, so a
    STATUS poll right after the socket appears legitimately answers STOPPED --
    which is not the same as "the start is not coming".
    """
    t0 = time.time()
    while time.time() - t0 < limit:
        st = status()
        if st == "STATUS:RUNNING":
            return st
        if "TRANSITIONING" in st:
            wait_final()
            continue
        if st in ("STATUS:STOPPED", "STATUS:EMPTY", "STATUS:ERROR"):
            # Either the boot start has not been claimed yet or it will not come;
            # asking explicitly settles it either way.
            r = cmd("START")
            if "OK" in r:
                wait_final()
            else:
                time.sleep(0.05)
            continue
        time.sleep(0.02)
    return status()


def set_switch(pos):
    with open(SWITCH, "w") as f:
        f.write(pos)


class Runtime:
    """One plc_main process, its log captured."""

    def __init__(self, tag, init_ms=2500, preload=None, env=None):
        self.tag = tag
        for p in (SOCK,):
            try:
                os.unlink(p)
            except OSError:
                pass
        self.logpath = f"/tmp/rt_{tag}.log"
        self.log = open(self.logpath, "w")
        e = dict(os.environ, FAKEVPP_INIT_MS=str(init_ms), FAKEVPP_SWITCH_FILE=SWITCH)
        if preload:
            e["LD_PRELOAD"] = preload
        if env:
            e.update(env)
        argv = ["stdbuf", "-oL", "-eL", "./build/plc_main", "--print-logs"]
        if os.environ.get("SUITE_GDB"):
            argv = ["gdb", "-q", "-batch",
                    "-ex", "handle SIGINT nostop noprint pass",
                    "-ex", "handle SIGTERM nostop noprint pass",
                    "-ex", "handle SIGUSR1 nostop noprint pass",
                    "-ex", "run", "-ex", "bt 12", "-ex", "thread apply all bt 4",
                    "--args", "./build/plc_main", "--print-logs"]
        self.p = subprocess.Popen(
            argv,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            env=e,
        )
        self.ready = False
        for _ in range(600):
            if self.p.poll() is not None:
                break
            try:
                if cmd("PING", timeout=2) == "PING:OK":
                    self.ready = True
                    break
            except OSError:
                pass
            time.sleep(0.01)
        if not self.ready:
            print(f"      runtime {tag} never answered PING")
            self.dump()

    def journal(self):
        self.log.flush()
        try:
            return open(self.logpath).read()
        except OSError:
            return ""

    def dump(self, n=25):
        print(f"      exit code: {self.p.poll()}  journal (socket noise removed):")
        lines = [l for l in self.journal().strip().splitlines()
                 if "Unix socket client" not in l]
        for line in lines[-n:]:
            print("       ", line)

    def maps(self):
        try:
            return open(f"/proc/{self.p.pid}/maps").read()
        except OSError:
            return ""

    def program_mapped(self):
        return "libplc_" in self.maps()

    def signal_and_wait(self, sig, limit=10.0):
        self.p.send_signal(sig)
        t0 = time.time()
        while time.time() - t0 < limit:
            rc = self.p.poll()
            if rc is not None:
                return rc, time.time() - t0
            time.sleep(0.02)
        return None, time.time() - t0

    def kill(self):
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait()
        self.log.close()


def record(finding, name, ok, detail=""):
    results.append((finding, name, ok))
    tail = f" -- {detail}" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] ({finding}) {name}{tail}")


def banner(t):
    print(f"\n=== {t}")


# ------------------------------------------------- findings 2 + 3: shutdown
def test_shutdown_midstart(sig, signame):
    """SIGINT/SIGTERM while TRANSITIONING_TO_RUN must complete, not hang.

    Pre-fix: unload_plc_program's guard only matched RUNNING so it published
    nothing, plc_cycle_thread then published RUNNING unconditionally, and the
    join in the teardown never returned.
    """
    banner(f"findings 2+3: {signame} during a start")
    set_switch("RUN")
    rt = Runtime(f"mid_{signame}", init_ms=2500)
    caught = wait_transitioning()
    st = status()
    record("2/3", f"{signame}: caught the start in flight", caught, f"state={st}")
    rc, took = rt.signal_and_wait(sig, limit=12.0)
    j = rt.journal()
    if rc is None:
        record("2/3", f"{signame}: process exits", False, "HUNG, needed SIGKILL")
    elif rc < 0:
        record("2/3", f"{signame}: process exits", False, f"died on signal {-rc}")
    else:
        record("2/3", f"{signame}: process exits", True, f"rc={rc} in {took:.2f}s")
    # Either the start was allowed to land and then torn down properly, or the
    # cycle thread declined to publish RUNNING over the teardown. What must never
    # happen is RUNNING published and no unload.
    resurrected = ("PLC State: RUNNING" in j) and ("PLC program unloaded successfully" not in j)
    record(
        "3",
        f"{signame}: RUNNING never left standing over a teardown",
        not resurrected,
        "declined to publish" if "not releasing the first scan" in j
        else ("landed then unloaded" if "unloaded successfully" in j else "no RUNNING published"),
    )
    record(
        "2",
        f"{signame}: shutdown waited for the transition",
        "Shutdown waited" in j or "not releasing the first scan" in j,
        "waited for the landing" if "Shutdown waited" in j else "cycle thread declined",
    )
    rt.kill()


# ------------------------------------------------ finding 4: phantom stop
def test_phantom_stop():
    """A STOP whose worker cannot be spawned must not report a stop that never ran.

    The claim has already published TRANSITIONING_TO_STOP, which ends the scan;
    pre-fix the failure path then published STOPPED while unload_plc_program never
    ran, leaving the .so mapped and the program loaded. /proc/<pid>/maps is the
    oracle: STATUS says STOPPED either way, only the mapping tells the truth.
    """
    banner("finding 4: STOP with the transition worker unspawnable")
    set_switch("RUN")
    rt = Runtime("phantom", init_ms=200, preload="/tmp/failinject.so")
    st = ensure_running()
    record("4", "reached RUNNING before the test", st == "STATUS:RUNNING", st)
    if st != "STATUS:RUNNING":
        rt.dump()
        record("4", "program mapped while RUNNING", False, "skipped, runtime not running")
        rt.kill()
        return
    record("4", "program mapped while RUNNING", rt.program_mapped())

    open(ARM, "w").close()          # next pthread_create fails, one-shot
    reply = cmd("STOP")
    st, secs = wait_final()
    mapped = rt.program_mapped()
    j = rt.journal()
    record("4", "STOP still lands a final state", "TRANSITIONING" not in st, f"{reply} -> {st}")
    record(
        "4",
        "the stop was real: program unmapped",
        not mapped,
        "still mapped -- STATUS lied about the stop" if mapped else f"unmapped, {st}",
    )
    fired = ("completing the transition on the calling thread" in j
             or "Failed to create transition thread" in j)
    record("4", "the spawn failure was actually injected", fired,
           "injection fired" if fired else "injection MISSED -- test inconclusive")
    record(
        "4",
        "teardown actually ran",
        "PLC program unloaded successfully" in j,
        "journal shows the unload" if "unloaded successfully" in j else "no unload in journal",
    )
    try:
        os.unlink(ARM)
    except OSError:
        pass
    rt.kill()


# ------------------------------- finding 5: switch intent must not be lost
def test_switch_intent():
    """A switch flip during a transition must not be lost.

    The sequence that matters, and the only one that records movement at all
    (plc_set_switch_position only notes EDGES):

      1. PLC RUNNING, switch RUN.
      2. Flip to STOP. The plugin stores STOP and requests a stop, which is
         accepted -- TRANSITIONING_TO_STOP.
      3. Flip back to RUN while that stop is still tearing down. The plugin
         stores RUN (an edge, so movement is recorded) and requests a start,
         which is DROPPED because a transition is in flight.
      4. The stop lands on STOPPED. Only the movement record can now honour the
         switch, and pre-fix that record was consumed before the corrective
         transition was known to have started, with the return value discarded.

    Step 3 needs a stop wide enough to aim at, hence FAKEVPP_STOP_MS: the fake
    plugin sleeps in stop_loop() while its switch watcher is still alive.
    """
    banner("finding 5: switch flipped back during a stop")
    set_switch("RUN")
    rt = Runtime("switch", init_ms=300, env={"FAKEVPP_STOP_MS": "1500"})
    st = ensure_running()
    record("5", "reached RUNNING before the test", st == "STATUS:RUNNING", st)
    if st != "STATUS:RUNNING":
        rt.dump()
        rt.kill()
        return

    disagreements = []
    corrections = 0
    for i in range(4):
        set_switch("STOP")                       # step 2
        if not wait_transitioning(limit=10):
            record("5", f"round {i}: stop transition started", False, "no transition seen")
            break
        time.sleep(0.2)                          # inside the slow teardown
        set_switch("RUN")                        # step 3: the dropped request
        st, _ = wait_final(limit=30)
        # Reconciliation runs after the landing and starts its own transition.
        time.sleep(0.3)
        st, _ = wait_final(limit=30)
        sw = cmd("SWITCH")
        agree = (sw == "SWITCH:RUN" and st == "STATUS:RUNNING") or (
            sw == "SWITCH:STOP" and st == "STATUS:STOPPED"
        )
        print(f"      round {i}: switch={sw} state={st} {'agree' if agree else 'DISAGREE'}")
        if not agree:
            disagreements.append((i, sw, st))
        corrections = rt.journal().count("correcting")
        if st != "STATUS:RUNNING":
            ensure_running()

    j = rt.journal()
    record(
        "5",
        "switch and PLC agree after every flip",
        not disagreements,
        f"{len(disagreements)} disagreement(s): {disagreements[:3]}" if disagreements
        else "4/4 rounds reconciled",
    )
    record(
        "5",
        "reconciliation is what did it",
        corrections > 0,
        f"{corrections} correction(s) logged",
    )
    rt.kill()


# ---------------------------------- shutdown with the PLC never having run
def test_shutdown_never_ran():
    """The graceful-shutdown segfault: Py_FinalizeEx() with no GIL held.

    main_tstate was only ever set by plugin_driver_start(), so a runtime whose PLC
    never ran had nothing to restore and finalised the interpreter without the
    GIL. Reached by booting with the switch in STOP, which is also the safe-mode
    and no-program shape.
    """
    banner("graceful shutdown when the PLC never ran")
    set_switch("STOP")
    for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
        rt = Runtime(f"never_{name}", init_ms=100)
        st, _ = wait_final(limit=30)
        never_ran = "RUNNING" not in st
        record("crash", f"{name}: PLC never ran", never_ran, st)
        rc, took = rt.signal_and_wait(sig, limit=10.0)
        if rc is None:
            record("crash", f"{name}: clean shutdown", False, "HUNG")
        elif rc < 0:
            record("crash", f"{name}: clean shutdown", False, f"died on signal {-rc}")
        else:
            record("crash", f"{name}: clean shutdown", True, f"rc={rc} in {took:.2f}s")
        rt.kill()
    set_switch("RUN")


# ------------------------- power-on with a program-shipped switch owner
class VppLayout:
    """Move the fake VPP from plugins.conf to vpp_plugins.conf, as a program upload ships it.

    `path`, when given, replaces the .so path of the moved entry.
    """

    def __init__(self, path=None):
        self.path = path

    def __enter__(self):
        with open("plugins.conf") as f:
            self.saved = f.read()
        lines = self.saved.splitlines(keepends=True)
        vpp = [l for l in lines if l.startswith("fakevpp,")]
        if self.path:
            vpp = [",".join([e[0], self.path] + e[2:]) for e in (l.split(",") for l in vpp)]
        with open("plugins.conf", "w") as f:
            f.writelines(l for l in lines if not l.startswith("fakevpp,"))
        with open("vpp_plugins.conf", "w") as f:
            f.writelines(vpp)
        return self

    def __exit__(self, *exc):
        with open("plugins.conf", "w") as f:
            f.write(self.saved)
        try:
            os.unlink("vpp_plugins.conf")
        except OSError:
            pass


def wait_status(want, limit=25.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        st = status()
        if st == want:
            return st
        time.sleep(0.05)
    return status()


def test_boot_switch_vpp():
    """A switch owner shipped in vpp_plugins.conf must gate the boot auto-start.

    Pre-fix the VPP plugin was only initialised inside the start transition, so the
    auto-start read the RUN default, the program loaded and scanned, and switch
    reconciliation stopped it afterwards. The zero-scan oracle is that the program
    was never loaded: no load in the journal and no mapping, since a program that
    was never loaded never ran a cycle.
    """
    banner("power-on with the switch owner in vpp_plugins.conf")
    owner = {"FAKEVPP_SWITCH_OWNER": "1"}
    with VppLayout():
        set_switch("STOP")
        rt = Runtime("boot_stop", init_ms=100, env=owner)
        time.sleep(3.0)                       # longer than a start takes here
        st = status()
        j = rt.journal()
        record("boot", "STOP at power-on: PLC left STOPPED", st == "STATUS:STOPPED", st)
        record("boot", "STOP at power-on: program never loaded (zero scans)",
               "Loading PLC application" not in j and not rt.program_mapped())
        record("boot", "STOP at power-on: no start/stop pair in the journal",
               "TRANSITIONING_TO_RUN" not in j and "PLC State: RUNNING" not in j
               and "correcting" not in j)
        record("boot", "STOP at power-on: journal says why",
               "Hardware mode switch is in STOP - PLC left stopped" in j)

        set_switch("RUN")
        st = wait_status("STATUS:RUNNING")
        record("boot", "switch moved to RUN after boot starts the PLC",
               st == "STATUS:RUNNING", st)
        set_switch("STOP")
        st = wait_status("STATUS:STOPPED")
        record("boot", "switch moved back to STOP stops it", st == "STATUS:STOPPED", st)
        rc, _ = rt.signal_and_wait(signal.SIGTERM)
        record("boot", "STOP at power-on: clean shutdown", rc == 0, f"rc={rc}")
        rt.kill()

        set_switch("RUN")
        rt = Runtime("boot_run", init_ms=100, env=owner)
        st = wait_status("STATUS:RUNNING")
        time.sleep(1.0)
        j = rt.journal()
        record("boot", "RUN at power-on: PLC RUNNING", status() == "STATUS:RUNNING", st)
        starts = j.count("TRANSITIONING_TO_RUN")
        record("boot", "RUN at power-on: exactly one start, no stop",
               starts == 1 and "TRANSITIONING_TO_STOP" not in j, f"{starts} start(s)")
        rc, _ = rt.signal_and_wait(signal.SIGTERM)
        record("boot", "RUN at power-on: clean shutdown", rc == 0, f"rc={rc}")
        rt.kill()


# ----------------------------------------- finding 6 + the interlock guard
def test_switch_readable_and_interlock():
    banner("finding 6 + interlock regression guard")
    set_switch("RUN")
    rt = Runtime("misc", init_ms=1500)
    caught = wait_transitioning()
    busy = answered = 0
    while "TRANSITIONING" in status():
        r = cmd("SWITCH")
        if r.startswith("SWITCH:"):
            answered += 1
        elif "BUSY" in r:
            busy += 1
        time.sleep(0.01)
    record("6", "SWITCH answered mid-transition", caught and answered > 0 and busy == 0,
           f"{answered} answered, {busy} BUSY")
    wait_final()

    tally = {}
    for _ in range(25):
        for c in ("START", "STOP"):
            r = cmd(c)
            tally[r] = tally.get(r, 0) + 1
        time.sleep(0.15)
    st, secs = wait_final()
    record("2/3", "no wedge after 25 rapid start/stop pairs", "TRANSITIONING" not in st,
           f"settled on {st} in {secs:.2f}s")
    record("2/3", "still answering after the stress", cmd("PING") == "PING:OK")
    rt.kill()


STOPFAIL_CONF_LINE = (
    "stopfail,/fixtures/stopfail/stopfail_plugin.py,1,0,/fixtures/stopfail/stopfail_config.json\n"
)
IMAGE_RELEASED = "[image_tables] cleared all pointers"


def stop_failed_line(name):
    return (
        f"Stop failed: plugin {name} did not stop; keeping the I/O image and entering ERROR"
        " until the runtime restarts"
    )


class PluginsConf:
    """Append entries to plugins.conf for one group, and restore the file on exit.

    `drop` holds line prefixes: the entries starting with one of them are left out meanwhile.
    """

    def __init__(self, *lines, drop=()):
        self.lines = lines
        self.drop = tuple(drop)

    def __enter__(self):
        with open("plugins.conf") as f:
            self.saved = f.read()
        kept = "".join(
            l for l in self.saved.splitlines(keepends=True) if not l.startswith(self.drop)
        )
        base = kept if kept.endswith("\n") or not kept else kept + "\n"
        with open("plugins.conf", "w") as f:
            f.write(base + "".join(self.lines))
        return self

    def __exit__(self, *exc):
        with open("plugins.conf", "w") as f:
            f.write(self.saved)


class StopFailPlugin(PluginsConf):
    """Append the enabled stopfail Python plugin to plugins.conf, and restore the file on exit."""

    def __init__(self):
        super().__init__(STOPFAIL_CONF_LINE)


def sigterm_exit(rt):
    """Send SIGTERM and wait for the exit. Returns (exited cleanly, detail)."""
    rc, took = rt.signal_and_wait(signal.SIGTERM)
    if rc is None:
        return False, "HUNG, needed SIGKILL"
    if rc < 0:
        return False, f"died on signal {-rc}"
    return True, f"rc={rc} in {took:.2f}s"


def stop_failed_case(tag, env, stop_marker):
    """Stop a PLC whose stopfail plugin refuses to stop and check the freeze and the latch.

    Returns (runtime, journal offset of the STOP, whether the PLC reached RUNNING first).
    The runtime is left alive for the caller.
    """
    set_switch("RUN")
    rt = Runtime(tag, init_ms=200, env=env)
    st = ensure_running()
    record("stopfail", f"{tag}: reached RUNNING before the test", st == "STATUS:RUNNING", st)
    if st != "STATUS:RUNNING":
        rt.dump()
        return rt, len(rt.journal()), False
    started = "Plugin stopfail started successfully" in rt.journal()
    record("stopfail", f"{tag}: the stopfail plugin was started", started)

    mark = len(rt.journal())
    reply = cmd("STOP")
    st, secs = wait_final()
    landed = f"{reply} -> {st} in {secs:.2f}s"
    record("stopfail", f"{tag}: STOP lands STATUS:ERROR", st == "STATUS:ERROR", landed)
    after = rt.journal()[mark:]
    fired = stop_marker in after
    called = "stop_loop ran" if fired else "stop_loop never ran -- test inconclusive"
    record("stopfail", f"{tag}: the failing stop_loop was actually called", fired, called)
    named = stop_failed_line("stopfail") in after
    record("stopfail", f"{tag}: journal names the plugin that did not stop", named)
    mapped = rt.program_mapped()
    unmapped = "mapped" if mapped else "unmapped -- the teardown ran past the failed stop"
    record("stopfail", f"{tag}: program library still mapped after the stop", mapped, unmapped)

    reply = cmd("START")
    refused = reply == "START:ERROR"
    record("stopfail", f"{tag}: START answers START:ERROR while latched", refused, reply)
    samples = []
    for _ in range(10):
        time.sleep(0.1)
        samples.append(status())
    stayed = all(s == "STATUS:ERROR" for s in samples)
    seen = ", ".join(sorted(set(samples)))
    record("stopfail", f"{tag}: STATUS stays STATUS:ERROR after the START", stayed, seen)

    released = IMAGE_RELEASED in rt.journal()[mark:]
    freed = "released -- the image was freed under a live plugin" if released else "kept"
    record("stopfail", f"{tag}: I/O image not released after the stop", not released, freed)
    return rt, mark, True


def stop_none_case(tag):
    """Stop a PLC whose stopfail plugin stops and returns None, and check the normal teardown.

    The runtime is left alive for the caller.
    """
    set_switch("RUN")
    rt = Runtime(tag, init_ms=200, env={"STOPFAIL_MODE": "none"})
    st = ensure_running()
    record("stopfail", f"{tag}: reached RUNNING before the test", st == "STATUS:RUNNING", st)
    if st != "STATUS:RUNNING":
        rt.dump()
        return rt
    started = "Plugin stopfail started successfully" in rt.journal()
    record("stopfail", f"{tag}: the stopfail plugin was started", started)

    mark = len(rt.journal())
    reply = cmd("STOP")
    st, secs = wait_final()
    landed = f"{reply} -> {st} in {secs:.2f}s"
    record("stopfail", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)
    after = rt.journal()[mark:]
    fired = "stopfail: stop_loop returning None" in after
    called = "stop_loop ran" if fired else "stop_loop never ran -- test inconclusive"
    record("stopfail", f"{tag}: the None-returning stop_loop was actually called", fired, called)
    stopped = "Plugin stopfail stopped successfully" in after
    record("stopfail", f"{tag}: journal reports the plugin stopped successfully", stopped)
    released = IMAGE_RELEASED in after
    record("stopfail", f"{tag}: I/O image released by the normal teardown", released)
    named = stop_failed_line("stopfail") in after
    record("stopfail", f"{tag}: journal has no stop-failed line", not named)
    mapped = rt.program_mapped()
    unmapped = "still mapped -- the teardown did not run" if mapped else "unmapped"
    record("stopfail", f"{tag}: program library unmapped after the stop", not mapped, unmapped)

    reply = cmd("START")
    st, secs = wait_final()
    restarted = reply == "START:OK" and st == "STATUS:RUNNING"
    landed = f"{reply} -> {st} in {secs:.2f}s"
    record("stopfail", f"{tag}: START after the stop is not latched", restarted, landed)
    reply = cmd("STOP")
    st, secs = wait_final()
    landed = f"{reply} -> {st} in {secs:.2f}s"
    record("stopfail", f"{tag}: a second STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)
    return rt


def test_stop_failed():
    """A plugin whose stop fails keeps the image, journal and library, and latches ERROR."""
    banner("a plugin that does not stop: image kept, ERROR latched until restart")
    with StopFailPlugin():
        rt, mark, ran = stop_failed_case(
            "stopfail_false", None, "stopfail: stop_loop returning False"
        )
        if not ran:
            exited, detail = False, "skipped, runtime not running"
        else:
            exited, detail = sigterm_exit(rt)
        record("stopfail", "SIGTERM while latched: process exits", exited, detail)
        if ran:
            released = IMAGE_RELEASED in rt.journal()[mark:]
            kept = "released" if released else "kept"
            name = "SIGTERM while latched: frozen image not released on the way out"
            record("stopfail", name, not released, kept)
        rt.kill()

        rt, _, _ = stop_failed_case(
            "stopfail_raise", {"STOPFAIL_MODE": "raise"}, "stopfail: stop_loop raising"
        )
        rt.kill()

        rt = stop_none_case("stopfail_none")
        rt.kill()


SWITCH_OWNER = {"FAKEVPP_SWITCH_OWNER": "1"}
FAKEVPP_SO = "./build/plugins/libfakevpp_plugin.so"
FAKEVPP_LEGACY_SO = "./build/plugins/libfakevpp_legacy_plugin.so"
FAKEVPP_CLEANUP = "fakevpp: cleanup"
FAKEVPP_INIT = "fake VPP initialised"
IMAGE_FREE_REFUSED = "refusing to free the image"
PTRPROBE_CONF_LINE = (
    "ptrprobe,./build/plugins/libptrprobe_plugin.so,1,1,/tmp/ptrprobe_config.json,\n"
)
PTRPROBE_NONE = "ptrprobe: image=none"
PTRPROBE_HELD = re.compile(r"ptrprobe: image=0x0*[1-9a-f][0-9a-f]*")
PYDETACH_CONF_LINE = (
    "pydetach,/fixtures/pydetach/pydetach_plugin.py,1,0,/fixtures/pydetach/pydetach_config.json\n"
)


def detached_line(name):
    return f"Plugin {name} detached from the I/O image"


def refused_line(name):
    return f"Plugin {name} did not detach; cleaning it up"


def wait_switch(want, limit=5.0):
    """Poll SWITCH until it answers `want`. Returns the last answer."""
    t0 = time.time()
    sw = "unreachable"
    while time.time() - t0 < limit:
        try:
            sw = cmd("SWITCH")
        except OSError:
            sw = "unreachable"
        if sw == want:
            return sw
        time.sleep(0.05)
    return sw


def scan_counts():
    """The scan_count of every task STATS lists, or None when STATS does not answer."""
    try:
        reply = cmd("STATS")
    except OSError:
        return None
    if not reply.startswith("STATS:"):
        return None
    return [int(n) for n in re.findall(r'"scan_count":(-?\d+)', reply)]


def exports_symbol(path, symbol):
    """Whether the shared object at `path` exports `symbol`, or None when it does not load."""
    try:
        return hasattr(ctypes.CDLL(path), symbol)
    except OSError:
        return None


def boot_switch_owner(tag, env=None, group="keepalive", init_ms=100):
    """Boot plc_main with the fake VPP owning the switch, switch on RUN, and wait for RUNNING.

    Returns (runtime, whether the PLC reached RUNNING).
    """
    set_switch("RUN")
    rt = Runtime(tag, init_ms=init_ms, env=dict(SWITCH_OWNER, **(env or {})))
    st = wait_status("STATUS:RUNNING")
    ok = st == "STATUS:RUNNING"
    record(group, f"{tag}: reached RUNNING before the test", ok, st)
    if not ok:
        rt.dump()
    return rt, ok


def stays_stopped(group, tag, seconds=3.0, after="the START"):
    """Poll STATUS and STATS every 100 ms for `seconds`: STOPPED at every poll, zero scans."""
    states, counts = [], []
    t0 = time.time()
    while time.time() - t0 < seconds:
        states.append(status())
        counts.append(scan_counts())
        time.sleep(0.1)
    stopped = all(s == "STATUS:STOPPED" for s in states)
    seen = ", ".join(sorted(set(states)))
    record(group, f"{tag}: STATUS:STOPPED at every poll for {seconds:g} s", stopped, seen)
    zero = all(c is not None and all(n == 0 for n in c) for c in counts)
    worst = max((max(c) for c in counts if c), default=0)
    missing = sum(1 for c in counts if c is None)
    detail = f"max scan_count {worst}, {missing} unanswered STATS of {len(counts)}"
    record(group, f"{tag}: zero scans after {after}", zero, detail)


def lands_then_stays_stopped(group, tag):
    """Wait for a refused start to land, record STOPPED, then check it stays stopped, no scan.

    A refusal after the plugin re-init is answered before the rollback, so STATUS can read
    the transition briefly after the reply.
    """
    st, secs = wait_final()
    landed = st == "STATUS:STOPPED"
    record(group, f"{tag}: the refused start lands STATUS:STOPPED", landed, f"{st} in {secs:.2f}s")
    stays_stopped(group, tag)


def socket_stop(rt):
    """Send STOP and wait for the final state. Returns (journal offset, final state, detail)."""
    mark = len(rt.journal())
    reply = cmd("STOP")
    st, secs = wait_final()
    return mark, st, f"{reply} -> {st} in {secs:.2f}s"


def keepalive_switch_case():
    """A keep-alive switch owner still reads the switch after a socket STOP."""
    tag = "keepalive_ac1"
    with VppLayout():
        rt, ran = boot_switch_owner(tag)
        if not ran:
            rt.kill()
            return
        mark, st, landed = socket_stop(rt)
        record("keepalive", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)
        after = rt.journal()[mark:]
        detached = detached_line("fakevpp") in after
        record("keepalive", f"{tag}: journal says fakevpp detached", detached)
        record("keepalive", f"{tag}: image freed on the stop", IMAGE_FREE_REFUSED not in after)

        set_switch("STOP")
        sw = wait_switch("SWITCH:STOP")
        record("keepalive", f"{tag}: switch moved to STOP is read", sw == "SWITCH:STOP", sw)
        window = rt.journal()[mark:]
        kept = FAKEVPP_CLEANUP not in window
        record("keepalive", f"{tag}: fakevpp not cleaned up by the stop", kept)
        reinit = FAKEVPP_INIT in window
        record("keepalive", f"{tag}: fakevpp not re-initialised by the stop", not reinit)

        set_switch("RUN")
        sw = wait_switch("SWITCH:RUN")
        record("keepalive", f"{tag}: switch moved back to RUN is read", sw == "SWITCH:RUN", sw)
        st = wait_status("STATUS:RUNNING")
        name = f"{tag}: switch moved to RUN starts the PLC with no socket START"
        record("keepalive", name, st == "STATUS:RUNNING", st)

        exited, detail = sigterm_exit(rt)
        record("keepalive", f"{tag}: clean shutdown after a keep-alive stop", exited, detail)
        rt.kill()


def keepalive_stale_switch_case():
    """A START after a stop does not use the switch position stored before the stop."""
    tag = "keepalive_abuse01"
    with VppLayout():
        rt, ran = boot_switch_owner(tag)
        if not ran:
            rt.kill()
            return
        _, st, landed = socket_stop(rt)
        record("keepalive", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)

        set_switch("STOP")
        wait_switch("SWITCH:STOP", limit=2.0)
        reply = cmd("START")
        refused = reply == "START:ERROR_SWITCH_STOP"
        record("keepalive", f"{tag}: START answers START:ERROR_SWITCH_STOP", refused, reply)
        stays_stopped("keepalive", tag)
        rt.kill()


def keepalive_refusal_case():
    """Fallback, refusal branch: a switch owner whose detach_image returns -1 is cleaned up."""
    tag = "keepalive_refuse"
    with VppLayout():
        rt, ran = boot_switch_owner(tag, {"FAKEVPP_FALLBACK": "1"})
        if not ran:
            rt.kill()
            return
        mark, st, landed = socket_stop(rt)
        record("keepalive", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)
        after = rt.journal()[mark:]
        called = "fakevpp: detach_image returning -1" in after
        record("keepalive", f"{tag}: the refusing detach_image was called", called)
        refused = refused_line("fakevpp") in after
        record("keepalive", f"{tag}: journal says fakevpp did not detach", refused)
        record("keepalive", f"{tag}: fakevpp: cleanup during the STOP", FAKEVPP_CLEANUP in after)
        exited, detail = sigterm_exit(rt)
        record("keepalive", f"{tag}: clean shutdown", exited, detail)
        rt.kill()


def keepalive_legacy_case():
    """Fallback, no-symbol branch: a switch owner without detach_image is cleaned up."""
    tag = "keepalive_legacy"
    exports = exports_symbol(FAKEVPP_SO, "detach_image")
    record("keepalive", f"{tag}: the default fake VPP exports detach_image", exports is True)
    exports = exports_symbol(FAKEVPP_LEGACY_SO, "detach_image")
    record("keepalive", f"{tag}: the legacy fake VPP has no detach_image", exports is False)
    with VppLayout(path=FAKEVPP_LEGACY_SO):
        rt, ran = boot_switch_owner(tag)
        if not ran:
            rt.kill()
            return
        mark, st, landed = socket_stop(rt)
        record("keepalive", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)
        after = rt.journal()[mark:]
        record("keepalive", f"{tag}: fakevpp: cleanup during the STOP", FAKEVPP_CLEANUP in after)
        claimed = detached_line("fakevpp") in after
        record("keepalive", f"{tag}: journal does not claim a detach", not claimed)
        exited, detail = sigterm_exit(rt)
        record("keepalive", f"{tag}: clean shutdown", exited, detail)
        rt.kill()


def keepalive_python_case():
    """Python path: pydetach detaches on STOP, then is cleaned up and re-initialised at START."""
    tag = "keepalive_python"
    with PluginsConf(PYDETACH_CONF_LINE):
        set_switch("RUN")
        rt = Runtime(tag, init_ms=100)
        st = ensure_running()
        record("keepalive", f"{tag}: reached RUNNING before the test", st == "STATUS:RUNNING", st)
        if st != "STATUS:RUNNING":
            rt.dump()
            rt.kill()
            return
        record("keepalive", f"{tag}: pydetach was initialised", "pydetach: init" in rt.journal())

        mark, st, landed = socket_stop(rt)
        record("keepalive", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)
        after = rt.journal()[mark:]
        record("keepalive", f"{tag}: pydetach detached on STOP", "pydetach: detached" in after)
        kept = "pydetach: cleanup" not in after
        record("keepalive", f"{tag}: pydetach not cleaned up on STOP", kept)

        mark = len(rt.journal())
        reply = cmd("START")
        st, secs = wait_final()
        landed = f"{reply} -> {st} in {secs:.2f}s"
        record("keepalive", f"{tag}: START lands STATUS:RUNNING", st == "STATUS:RUNNING", landed)
        after = rt.journal()[mark:]
        cleaned = after.find("pydetach: cleanup")
        inited = after.find("pydetach: init", cleaned + 1) if cleaned >= 0 else -1
        order = f"cleanup at {cleaned}, init after it at {inited}"
        name = f"{tag}: START cleans pydetach up, then initialises it"
        record("keepalive", name, cleaned >= 0 and inited > cleaned, order)
        rt.kill()


def test_keepalive():
    """A plugin that detaches stays alive across a stop; one that cannot is cleaned up."""
    banner("keep-alive across a stop: detach_image")
    keepalive_switch_case()
    keepalive_stale_switch_case()
    keepalive_refusal_case()
    keepalive_legacy_case()
    keepalive_python_case()
    set_switch("RUN")


def test_ptrprobe_cycles(cycles=50):
    """Stop/start cycling never leaves a surviving plugin holding a freed image.

    Every STOP must have ptrprobe drop its image before the runtime clears the image, and
    every START must hand it a non-null image through a fresh init().
    """
    banner(f"ptrprobe: {cycles} STOP/START cycles")
    with PluginsConf(PTRPROBE_CONF_LINE):
        set_switch("RUN")
        rt = Runtime("ptrprobe", init_ms=50)
        st = ensure_running()
        record("ptrprobe", "reached RUNNING before the test", st == "STATUS:RUNNING", st)
        if st != "STATUS:RUNNING":
            rt.dump()
            rt.kill()
            return
        held = PTRPROBE_HELD.search(rt.journal()) is not None
        record("ptrprobe", "ptrprobe holds a non-null image while RUNNING", held)

        bad_state, bad_stop, bad_start = [], [], []
        for i in range(cycles):
            mark = len(rt.journal())
            reply = cmd("STOP")
            st, _ = wait_final()
            if st != "STATUS:STOPPED":
                bad_state.append((i, reply, st))
            after = rt.journal()[mark:]
            none_at = after.find(PTRPROBE_NONE)
            cleared_at = after.find(IMAGE_RELEASED)
            if not (0 <= none_at < cleared_at):
                bad_stop.append((i, none_at, cleared_at))

            mark = len(rt.journal())
            reply = cmd("START")
            st, _ = wait_final()
            if st != "STATUS:RUNNING":
                bad_state.append((i, reply, st))
            if PTRPROBE_HELD.search(rt.journal()[mark:]) is None:
                bad_start.append(i)
            if rt.p.poll() is not None:
                bad_state.append((i, "process exited", rt.p.poll()))
                break

        name = f"all {cycles} cycles land STOPPED, then RUNNING"
        record("ptrprobe", name, not bad_state, f"{len(bad_state)} bad: {bad_state[:3]}")
        name = "every STOP: ptrprobe holds no image before the image is cleared"
        detail = f"{len(bad_stop)}/{cycles} failed (cycle, none at, cleared at): {bad_stop[:3]}"
        record("ptrprobe", name, not bad_stop, detail)
        name = "every START: ptrprobe holds a non-null image"
        detail = f"{len(bad_start)}/{cycles} failed: {bad_start[:5]}"
        record("ptrprobe", name, not bad_start, detail)
        exited, detail = sigterm_exit(rt)
        record("ptrprobe", "clean shutdown after the cycles", exited, detail)
        rt.kill()


PLC_SWITCH_REPORT_WAIT_S = 2.0
NO_REPORT_LINE = "no switch owner has reported a position"
CORRECTION_LINE = "Mode switch came to rest in STOP but the PLC landed on RUNNING"
FAKEVPP_ENTRY = "fakevpp,"
FAKEVPP_SILENT = {"FAKEVPP_SWITCH_SILENT": "1"}
FAKEVPP_SILENT_MARK = "fakevpp: switch silent, no position reported"
FAKEVPP_FALLBACK = {"FAKEVPP_FALLBACK": "1"}
PYDETACH_OWNER = {"PYDETACH_OWNS_SWITCH": "1"}
REINIT_MS = 1000


def timed_cmd(c):
    """Send `c` and return (reply, seconds until the reply arrived)."""
    t0 = time.time()
    reply = cmd(c)
    return reply, time.time() - t0


def wait_journal(rt, needle, limit):
    """Poll the journal until it holds `needle`. Returns whether it did within `limit` seconds."""
    t0 = time.time()
    while time.time() - t0 < limit:
        if needle in rt.journal():
            return True
        time.sleep(0.1)
    return False


def settle_stopped(rt, limit=PLC_SWITCH_REPORT_WAIT_S + 4):
    """Wait for the boot decision, then leave the PLC STOPPED. Returns the final state.

    A boot held by an unreported switch owner logs NO_REPORT_LINE and stays STOPPED. A boot
    that ignores the owner auto-starts and is stopped here, so the START under test begins
    from STOPPED either way.
    """
    t0 = time.time()
    while time.time() - t0 < limit:
        if NO_REPORT_LINE in rt.journal():
            break
        if status() == "STATUS:RUNNING":
            cmd("STOP")
            break
        time.sleep(0.1)
    st, _ = wait_final()
    return st


def gate_known_owner_case():
    """Known owner: a current STOP report refuses START before the transition begins."""
    tag = "gate_known_owner"
    with VppLayout():
        rt, ran = boot_switch_owner(tag, group="gate")
        if not ran:
            rt.kill()
            return
        _, st, landed = socket_stop(rt)
        record("gate", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)
        set_switch("STOP")
        sw = wait_switch("SWITCH:STOP", limit=2.0)
        record("gate", f"{tag}: the keep-alive owner reports STOP", sw == "SWITCH:STOP", sw)

        mark = len(rt.journal())
        reply = cmd("START")
        refused = reply == "START:ERROR_SWITCH_STOP"
        record("gate", f"{tag}: START answers START:ERROR_SWITCH_STOP", refused, reply)
        stays_stopped("gate", tag)
        after = rt.journal()[mark:]
        signs = ("TRANSITIONING_TO_RUN", "Loading PLC application", FAKEVPP_CLEANUP, FAKEVPP_INIT)
        begun = [sign for sign in signs if sign in after]
        name = f"{tag}: START not begun: no transition, no plugin re-init"
        record("gate", name, not begun, ", ".join(begun) or "none")
        rt.kill()


def gate_legacy_stop_case():
    """Legacy owner: a fallback owner reporting STOP during the start's re-init stops it."""
    tag = "gate_legacy_stop"
    exports = exports_symbol(FAKEVPP_LEGACY_SO, "owns_mode_switch")
    record("gate", f"{tag}: the legacy fake VPP has no owns_mode_switch", exports is False)
    with VppLayout(path=FAKEVPP_LEGACY_SO):
        rt, ran = boot_switch_owner(tag, group="gate")
        if not ran:
            rt.kill()
            return
        _, st, landed = socket_stop(rt)
        record("gate", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)

        set_switch("STOP")
        mark = len(rt.journal())
        reply = cmd("START")
        refused = reply == "START:ERROR_SWITCH_STOP"
        record("gate", f"{tag}: START answers START:ERROR_SWITCH_STOP", refused, reply)
        lands_then_stays_stopped("gate", tag)
        corrected = CORRECTION_LINE in rt.journal()[mark:]
        record("gate", f"{tag}: no switch correction after the START", not corrected)
        rt.kill()


def gate_legacy_recovery_case():
    """A fallback owner reads unknown after a stop; a START in RUN runs the PLC again."""
    tag = "gate_legacy_recover"
    with VppLayout(path=FAKEVPP_LEGACY_SO):
        rt, ran = boot_switch_owner(tag, group="gate", init_ms=REINIT_MS)
        if not ran:
            rt.kill()
            return
        _, st, landed = socket_stop(rt)
        record("gate", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)
        sw = wait_switch("SWITCH:UNKNOWN", limit=2.0)
        name = f"{tag}: SWITCH answers SWITCH:UNKNOWN after the stop"
        record("gate", name, sw == "SWITCH:UNKNOWN", sw)

        set_switch("RUN")
        reply, secs = timed_cmd("START")
        record("gate", f"{tag}: START answers START:OK", reply == "START:OK", reply)
        name = f"{tag}: the reply comes after the plugin re-init"
        detail = f"{secs:.2f}s, the re-init sleeps {REINIT_MS} ms"
        record("gate", name, secs >= 0.8 * REINIT_MS / 1000, detail)
        st = wait_status("STATUS:RUNNING")
        alive = rt.p.poll() is None
        name = f"{tag}: STATUS reaches RUNNING in the same process"
        record("gate", name, st == "STATUS:RUNNING" and alive, f"{st}, alive={alive}")
        rt.kill()


def gate_no_owner_case():
    """With no switch owner a start is not gated and SWITCH reads RUN."""
    tag = "gate_no_owner"
    with PluginsConf(drop=(FAKEVPP_ENTRY,)):
        set_switch("RUN")
        rt = Runtime(tag, init_ms=100)
        st = ensure_running()
        record("gate", f"{tag}: reached RUNNING before the test", st == "STATUS:RUNNING", st)
        if st != "STATUS:RUNNING":
            rt.dump()
            rt.kill()
            return
        _, st, landed = socket_stop(rt)
        record("gate", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)

        reply = cmd("START")
        st, secs = wait_final()
        landed = f"{reply} -> {st} in {secs:.2f}s"
        ok = reply == "START:OK" and st == "STATUS:RUNNING"
        record("gate", f"{tag}: START answers START:OK and lands RUNNING", ok, landed)
        name = f"{tag}: no switch-report wait without an owner"
        record("gate", name, secs < PLC_SWITCH_REPORT_WAIT_S - 0.5, f"{secs:.2f}s")
        sw = cmd("SWITCH")
        record("gate", f"{tag}: SWITCH answers SWITCH:RUN", sw == "SWITCH:RUN", sw)
        rt.kill()


def gate_silent_owner_case():
    """Socket: START with an owner that never reports is refused after the wait."""
    tag = "gate_abuse05"
    set_switch("RUN")
    rt = Runtime(tag, init_ms=100, env=FAKEVPP_SILENT)
    st = settle_stopped(rt)
    record("gate", f"{tag}: STOPPED before the START", st == "STATUS:STOPPED", st)
    record("gate", f"{tag}: the fake VPP runs silent", FAKEVPP_SILENT_MARK in rt.journal())
    if st != "STATUS:STOPPED":
        rt.dump()
        rt.kill()
        return

    mark = len(rt.journal())
    reply, secs = timed_cmd("START")
    refused = reply == "START:ERROR_SWITCH_UNKNOWN"
    detail = f"{reply} in {secs:.2f}s"
    record("gate", f"{tag}: START answers START:ERROR_SWITCH_UNKNOWN", refused, detail)
    name = f"{tag}: refused once the 2 s switch-report wait has elapsed"
    record("gate", name, secs >= PLC_SWITCH_REPORT_WAIT_S - 0.1, f"{secs:.2f}s")
    lands_then_stays_stopped("gate", tag)
    sw = cmd("SWITCH")
    record("gate", f"{tag}: SWITCH answers SWITCH:UNKNOWN", sw == "SWITCH:UNKNOWN", sw)
    logged = NO_REPORT_LINE in rt.journal()[mark:]
    record("gate", f"{tag}: journal says no switch owner has reported a position", logged)
    rt.kill()


def gate_fallback_stop_case():
    """A declared fallback owner reporting STOP during the start's re-init refuses it."""
    tag = "gate_abuse07"
    exports = exports_symbol(FAKEVPP_SO, "owns_mode_switch")
    record("gate", f"{tag}: the fake VPP exports owns_mode_switch", exports is True)
    with VppLayout():
        rt, ran = boot_switch_owner(tag, FAKEVPP_FALLBACK, group="gate")
        if not ran:
            rt.kill()
            return
        _, st, landed = socket_stop(rt)
        record("gate", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)

        set_switch("STOP")
        reply, secs = timed_cmd("START")
        refused = reply == "START:ERROR_SWITCH_STOP"
        detail = f"{reply} in {secs:.2f}s"
        record("gate", f"{tag}: START answers START:ERROR_SWITCH_STOP", refused, detail)
        name = f"{tag}: answered within the 2 s switch-report wait"
        record("gate", name, secs < PLC_SWITCH_REPORT_WAIT_S, f"{secs:.2f}s")
        lands_then_stays_stopped("gate", tag)
        rt.kill()


def gate_python_owner_case():
    """Python declaration: pydetach declares ownership, never reports, and START is refused."""
    tag = "gate_python_owner"
    with PluginsConf(PYDETACH_CONF_LINE, drop=(FAKEVPP_ENTRY,)):
        set_switch("RUN")
        rt = Runtime(tag, init_ms=100, env=PYDETACH_OWNER)
        st = settle_stopped(rt)
        record("gate", f"{tag}: STOPPED before the START", st == "STATUS:STOPPED", st)
        record("gate", f"{tag}: pydetach was initialised", "pydetach: init" in rt.journal())
        if st != "STATUS:STOPPED":
            rt.dump()
            rt.kill()
            return
        reply = cmd("START")
        refused = reply == "START:ERROR_SWITCH_UNKNOWN"
        record("gate", f"{tag}: START answers START:ERROR_SWITCH_UNKNOWN", refused, reply)
        rt.kill()


def gate_two_owners_case():
    """Every owner must report: one owner reporting RUN does not cover a silent declared owner.

    The fake VPP reports RUN from init(), so it owns the switch implicitly; pydetach declares
    ownership and never reports. A report credited to the wrong plugin would read RUN here.
    """
    tag = "gate_two_owners"
    with PluginsConf(PYDETACH_CONF_LINE):
        set_switch("RUN")
        rt = Runtime(tag, init_ms=100, env=PYDETACH_OWNER)
        st = settle_stopped(rt)
        record("gate", f"{tag}: STOPPED before the START", st == "STATUS:STOPPED", st)
        j = rt.journal()
        both = FAKEVPP_INIT in j and "pydetach: init" in j
        record("gate", f"{tag}: the fake VPP and pydetach were initialised", both)
        if st != "STATUS:STOPPED":
            rt.dump()
            rt.kill()
            return
        reply = cmd("START")
        refused = reply == "START:ERROR_SWITCH_UNKNOWN"
        record("gate", f"{tag}: START answers START:ERROR_SWITCH_UNKNOWN", refused, reply)
        sw = cmd("SWITCH")
        record("gate", f"{tag}: SWITCH answers SWITCH:UNKNOWN", sw == "SWITCH:UNKNOWN", sw)
        rt.kill()


SWITCH_STOP_REFUSED_LINE = "Start refused: the mode switch is in STOP"
FAKEVPP_SWITCH_SILENT_TEXT = "SILENT"


def start_then(rt, needle, action, limit=10.0):
    """Send START, run `action` once the journal holds `needle`, then read the reply.

    Returns (reply, whether `needle` appeared within `limit` seconds, seconds from the send
    to the reply).
    """
    mark = len(rt.journal())
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)
    s.connect(SOCK)
    t0 = time.time()
    try:
        s.sendall(b"START\n")
        seen = False
        while time.time() - t0 < limit:
            if needle in rt.journal()[mark:]:
                seen = True
                break
            time.sleep(0.01)
        action()
        try:
            reply = s.recv(4096).decode().strip()
        except OSError as exc:
            reply = f"no reply: {exc}"
    finally:
        s.close()
    return reply, seen, time.time() - t0


def gate_reinit_stop_case():
    """Keep-alive owner: a STOP reported during the start's re-init refuses the socket START."""
    tag = "gate_reinit_stop"
    with VppLayout():
        rt, ran = boot_switch_owner(tag, group="gate", init_ms=REINIT_MS)
        if not ran:
            rt.kill()
            set_switch("RUN")
            return
        _, st, landed = socket_stop(rt)
        record("gate", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)
        sw = wait_switch("SWITCH:RUN")
        name = f"{tag}: SWITCH answers SWITCH:RUN before the START"
        record("gate", name, sw == "SWITCH:RUN", sw)

        mark = len(rt.journal())
        reply, seen, secs = start_then(rt, "Loading PLC application", lambda: set_switch("STOP"))
        name = f"{tag}: the START began its transition before the switch moved to STOP"
        record("gate", name, seen)
        refused = reply == "START:ERROR_SWITCH_STOP"
        detail = f"{reply} in {secs:.2f}s"
        record("gate", f"{tag}: START answers START:ERROR_SWITCH_STOP", refused, detail)
        lands_then_stays_stopped("gate", tag)
        logged = SWITCH_STOP_REFUSED_LINE in rt.journal()[mark:]
        record("gate", f"{tag}: journal says the start was refused for the switch in STOP", logged)
        rt.kill()
    set_switch("RUN")


def gate_reinit_silent_case():
    """Keep-alive owner: an owner silent after the start's re-init refuses the socket START."""
    tag = "gate_reinit_silent"
    with VppLayout():
        rt, ran = boot_switch_owner(tag, group="gate", init_ms=300)
        if not ran:
            rt.kill()
            set_switch("RUN")
            return
        _, st, landed = socket_stop(rt)
        record("gate", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)
        sw = wait_switch("SWITCH:RUN")
        name = f"{tag}: SWITCH answers SWITCH:RUN before the switch goes silent"
        record("gate", name, sw == "SWITCH:RUN", sw)

        set_switch(FAKEVPP_SWITCH_SILENT_TEXT)
        time.sleep(0.3)
        sw = cmd("SWITCH")
        name = f"{tag}: SWITCH still answers SWITCH:RUN once the switch is silent"
        record("gate", name, sw == "SWITCH:RUN", sw)

        mark = len(rt.journal())
        reply, secs = timed_cmd("START")
        refused = reply == "START:ERROR_SWITCH_UNKNOWN"
        detail = f"{reply} in {secs:.2f}s"
        record("gate", f"{tag}: START answers START:ERROR_SWITCH_UNKNOWN", refused, detail)
        name = f"{tag}: refused once the 2 s switch-report wait has elapsed"
        record("gate", name, secs >= PLC_SWITCH_REPORT_WAIT_S - 0.1, f"{secs:.2f}s")
        lands_then_stays_stopped("gate", tag)
        sw = cmd("SWITCH")
        record("gate", f"{tag}: SWITCH answers SWITCH:UNKNOWN", sw == "SWITCH:UNKNOWN", sw)
        logged = NO_REPORT_LINE in rt.journal()[mark:]
        record("gate", f"{tag}: journal says no switch owner has reported a position", logged)
        rt.kill()
    set_switch("RUN")


def gate_refusal_first_case():
    """A switch refusal is answered before the rollback's slow cleanup() finishes."""
    tag = "gate_refusal_first"
    with VppLayout():
        env = dict(FAKEVPP_FALLBACK, FAKEVPP_CLEANUP_MS="1500")
        rt, ran = boot_switch_owner(tag, env, group="gate")
        if not ran:
            rt.kill()
            set_switch("RUN")
            return
        _, st, landed = socket_stop(rt)
        record("gate", f"{tag}: STOP lands STATUS:STOPPED", st == "STATUS:STOPPED", landed)

        set_switch("STOP")
        mark = len(rt.journal())
        reply = cmd("START")
        after = status()
        refused = reply == "START:ERROR_SWITCH_STOP"
        record("gate", f"{tag}: START answers START:ERROR_SWITCH_STOP", refused, reply)
        name = f"{tag}: STATUS right after the reply answers STATUS:TRANSITIONING"
        record("gate", name, after == "STATUS:TRANSITIONING", after)
        lands_then_stays_stopped("gate", tag)
        cleaned = FAKEVPP_CLEANUP in rt.journal()[mark:]
        record("gate", f"{tag}: the rollback ran fakevpp: cleanup", cleaned)
        rt.kill()
    set_switch("RUN")


def test_gate():
    """No start runs a scan while the switch reads STOP or an owner has not reported."""
    banner("start gate: every start honours the switch")
    gate_known_owner_case()
    gate_legacy_stop_case()
    gate_legacy_recovery_case()
    gate_no_owner_case()
    gate_silent_owner_case()
    gate_fallback_stop_case()
    gate_python_owner_case()
    gate_two_owners_case()
    gate_reinit_stop_case()
    gate_reinit_silent_case()
    gate_refusal_first_case()
    set_switch("RUN")


def test_boot_gate():
    """A declared switch owner that never reports holds the boot auto-start."""
    banner("power-on with a switch owner that never reports")
    tag = "bootgate_abuse05"
    with VppLayout():
        set_switch("RUN")
        rt = Runtime(tag, init_ms=100, env=FAKEVPP_SILENT)
        decided = wait_journal(rt, NO_REPORT_LINE, limit=PLC_SWITCH_REPORT_WAIT_S + 4)
        name = f"{tag}: journal says no switch owner has reported a position"
        record("bootgate", name, decided)
        stays_stopped("bootgate", tag, after="power-on")
        sw = cmd("SWITCH")
        record("bootgate", f"{tag}: SWITCH answers SWITCH:UNKNOWN", sw == "SWITCH:UNKNOWN", sw)
        j = rt.journal()
        record("bootgate", f"{tag}: the fake VPP runs silent", FAKEVPP_SILENT_MARK in j)
        loaded = "Loading PLC application" in j or rt.program_mapped()
        record("bootgate", f"{tag}: program never loaded", not loaded)
        rt.kill()


ONLY = os.environ.get("SUITE_ONLY", "")


def run(name, fn, *a):
    if not ONLY or ONLY == name:
        fn(*a)


run("crash", test_shutdown_never_ran)
run("shutdown", test_shutdown_midstart, signal.SIGINT, "SIGINT")
run("shutdown", test_shutdown_midstart, signal.SIGTERM, "SIGTERM")
run("phantom", test_phantom_stop)
run("switch", test_switch_intent)
run("misc", test_switch_readable_and_interlock)
run("bootswitch", test_boot_switch_vpp)
run("stopfail", test_stop_failed)
run("keepalive", test_keepalive)
run("ptrprobe", test_ptrprobe_cycles)
run("gate", test_gate)
run("bootgate", test_boot_gate)

print("\n=== summary")
by_finding = {}
for finding, name, ok in results:
    d = by_finding.setdefault(finding, [0, 0])
    d[0 if ok else 1] += 1
for finding in sorted(by_finding):
    p, f = by_finding[finding]
    print(f"  finding {finding:<6} {p} passed, {f} failed")
failed = sum(1 for _, _, ok in results if not ok)
print(f"  TOTAL: {len(results) - failed} passed, {failed} failed")
sys.exit(1 if failed else 0)
