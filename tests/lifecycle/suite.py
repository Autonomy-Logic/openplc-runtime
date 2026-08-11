"""PR #162 review findings: one test per finding, run against a real runtime.

Each test drives plc_main directly over its command socket, with the fake VPP
plugin providing a slow bring-up (so a transition is wide enough to aim at) and a
file-driven mode switch. Oracles are chosen to be observable from outside the
process -- socket replies, the journal, and /proc/<pid>/maps -- so the same suite
scores the pristine branch and the fixed one.
"""

import os
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
