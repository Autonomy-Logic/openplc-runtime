# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Scan-time benchmark of the MQTT integration (1.000 leaves changing every 10 ms scan).

Usage: bench.py <bench image> <payload dir> <none|generic|sparkplug> <seconds> <output dir>

The runtime container gets CPUs 0-3; the broker and the subscriber run on other
CPUs. Every scan body is traced by the relinked plc_main (scan_trace.c); the
percentiles are computed over the measurement window only, after a warm-up.
"""

from __future__ import annotations

import json
import platform
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "e2e" / "mqtt"))

# pylint: disable=wrong-import-position
import itertools

from e2e_env import (
    DEFAULT_MOSQUITTO,
    SPIKE_IMAGE,
    Network,
    Runtime,
    docker,
)

# pylint: enable=wrong-import-position

WARMUP_S = 30
PERIOD_US = 10_000
TRACE = "/var/run/runtime/scan_trace.bin"
RUNTIME_CPUS = "0-3"
SIDE_CPUS = "6-9"


def _int(text: str) -> int:
    return int(text.strip() or "0")


def _plc_cpu_ticks(runtime: Runtime) -> int:
    finder = 'for p in /proc/[0-9]*; do [ "$(readlink $p/exe)" = /workdir/build/plc_main ] && echo ${p#/proc/}; done'
    pid = runtime.exec("sh", "-c", finder).split()[0]
    fields = runtime.exec("cat", f"/proc/{pid}/stat").rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])


def _cgroup_usage_us(runtime: Runtime) -> int:
    for line in runtime.exec("cat", "/sys/fs/cgroup/cpu.stat").splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    return 0


def _trace_count(runtime: Runtime) -> int:
    return _int(runtime.exec("stat", "-c", "%s", TRACE)) // 8


def _percentile(sorted_ns: list[int], q: float) -> float:
    index = min(len(sorted_ns) - 1, max(0, round(q * (len(sorted_ns) - 1))))
    return sorted_ns[index] / 1000.0


def main(image: str, payloads: Path, mode: str, seconds: int, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    network = Network(DEFAULT_MOSQUITTO)
    network.up()
    docker("update", "--cpuset-cpus", SIDE_CPUS, network.broker)
    runtime = Runtime(
        image,
        network,
        ("--cpuset-cpus", RUNTIME_CPUS, "-e", f"OPENPLC_SCAN_TRACE={TRACE}"),
    )
    consumer = f"{network.name}-bench-consumer"
    try:
        runtime.up()
        payload = {"none": "bench-none", "generic": "bench-generic", "sparkplug": "bench-sparkplug"}
        result = runtime.deploy(payloads / payload[mode])
        assert result.get("build") == "SUCCESS", result
        runtime.start_plc()
        time.sleep(WARMUP_S)
        if mode != "none":
            docker(
                "run", "-d", "--name", consumer, "--network", network.name,
                "--cpuset-cpus", SIDE_CPUS, "-v", f"{HERE}:/bench:ro", "-v", f"{out}:/out",
                SPIKE_IMAGE, "python", "/bench/bench_consumer.py", "broker",
                "generic" if mode == "generic" else "sparkplug", str(seconds), "/out/consumer.json",
            )  # fmt: skip
        start_stats = runtime.scan_stats()
        start_mqtt = runtime.mqtt_status()
        start_count = _trace_count(runtime)
        start_ticks = _plc_cpu_ticks(runtime)
        start_cg = _cgroup_usage_us(runtime)
        started = time.monotonic()
        time.sleep(seconds)
        end_stats = runtime.scan_stats()
        end_mqtt = runtime.mqtt_status()
        elapsed = time.monotonic() - started
        end_ticks = _plc_cpu_ticks(runtime)
        end_cg = _cgroup_usage_us(runtime)
        time.sleep(2)
        end_count = _trace_count(runtime)
        docker("cp", f"{runtime.name}:{TRACE}", str(out / "scan_trace.bin"))
        raw = (out / "scan_trace.bin").read_bytes()
        pairs = list(struct.iter_unpack("<II", raw[: len(raw) // 8 * 8]))[start_count:end_count]
        window = sorted(body for _, body in pairs)
        starts = [start for start, _ in pairs]
        missed = sum(max(0, round((b - a) / PERIOD_US) - 1) for a, b in itertools.pairwise(starts))
        late_bodies = sum(1 for body in window if body > PERIOD_US * 1000)
        ticks_per_s = _int(runtime.exec("getconf", "CLK_TCK"))
        if mode != "none":
            assert (
                subprocess.run(
                    ["docker", "wait", consumer], capture_output=True, check=False, timeout=120
                ).returncode
                == 0
            )
        summary = {
            "mode": mode,
            "image": image,
            "seconds": round(elapsed, 1),
            "scans_traced": len(window),
            "scan_us": {
                "p50": _percentile(window, 0.50),
                "p95": _percentile(window, 0.95),
                "p99": _percentile(window, 0.99),
                "p99_9": _percentile(window, 0.999),
                "max": window[-1] / 1000.0,
                "mean": round(statistics.fmean(window) / 1000.0, 3),
            },
            "missed_cycles": missed,
            "bodies_over_period": late_bodies,
            "max_start_gap_us": max(b - a for a, b in itertools.pairwise(starts)),
            "overruns": end_stats["overruns"] - start_stats["overruns"],  # type: ignore[operator]
            "scan_count": end_stats["scan_count"] - start_stats["scan_count"],  # type: ignore[operator]
            "cycle_latency_max_us": end_stats["cycle_latency_max"],
            "cycle_time_max_us": end_stats["cycle_time_max"],
            "plc_main_cpu_percent": round(
                100 * (end_ticks - start_ticks) / ticks_per_s / elapsed, 1
            ),
            "container_cpu_percent": round(100 * (end_cg - start_cg) / 1e6 / elapsed, 1),
            "mqtt_start": start_mqtt,
            "mqtt_end": end_mqtt,
            "environment": {
                "host": platform.platform(),
                "docker": docker("info", "--format", "{{.OperatingSystem}} {{.KernelVersion}} "
                                 "{{.Architecture}} cpus={{.NCPU}} mem={{.MemTotal}}").strip(),
                "runtime_cpus": RUNTIME_CPUS,
                "broker": network.broker_version(),
                "uname": runtime.exec("uname", "-a").strip(),
                "cpu": runtime.exec("sh", "-c", "lscpu | grep -E 'Model name|^CPU\\(s\\)'").strip(),
            },
        }  # fmt: skip
        if mode != "none":
            summary["consumer"] = json.loads((out / "consumer.json").read_text())
        (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        (out / "runtime.log").write_text(runtime.logs(), encoding="utf-8")
        (out / "scan_trace.bin").unlink()
        keys = ("mode", "scan_us", "missed_cycles", "bodies_over_period", "max_start_gap_us")
        print(json.dumps({k: summary[k] for k in keys + ("overruns", "scan_count")}))
    finally:
        docker("rm", "-f", consumer, check=False)
        runtime.cleanup()
        network.cleanup()


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), Path(sys.argv[5]))
