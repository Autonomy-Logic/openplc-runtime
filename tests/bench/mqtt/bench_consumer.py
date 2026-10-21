# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Benchmark subscriber: counts what reaches the broker and checks every counter only grows.

Usage: bench_consumer.py <broker host> <generic|sparkplug> <seconds> <output json>
Runs inside the spike image (paho, Sparkplug gencode at /opt/gen); shares no code with the product.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import paho.mqtt.client as paho
import sparkplug_b_pb2 as pb  # pylint: disable=import-error


class Tally:  # pylint: disable=too-many-instance-attributes
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.lock = threading.Lock()
        self.messages = 0
        self.values = 0
        self.regressions = 0
        self.decode_errors = 0
        self.births = 0
        self.deaths = 0
        self.seq_gaps = 0
        self.last: dict[str, int] = {}
        self.last_seq: int | None = None
        self.per_second: dict[int, int] = {}

    def _value(self, key: str, value: int) -> None:
        self.values += 1
        previous = self.last.get(key)
        if previous is not None and value <= previous:
            self.regressions += 1
        self.last[key] = value

    def on_message(self, _client: paho.Client, _userdata: object, msg: paho.MQTTMessage) -> None:
        second = int(time.monotonic())
        with self.lock:
            self.messages += 1
            self.per_second[second] = self.per_second.get(second, 0) + 1
            try:
                if self.mode == "generic":
                    self._value(msg.topic, int(json.loads(msg.payload)["value"]))
                    return
                kind = msg.topic.split("/")[2]
                if kind == "NBIRTH":
                    self.births += 1
                    self.last_seq = 0
                    return
                if kind == "NDEATH":
                    self.deaths += 1
                    return
                if kind != "NDATA":
                    return
                payload = pb.Payload()
                payload.ParseFromString(msg.payload)
                if self.last_seq is not None and payload.seq != (self.last_seq + 1) % 256:
                    self.seq_gaps += 1
                self.last_seq = payload.seq
                for metric in payload.metrics:
                    self._value(metric.name, metric.int_value)
            except (ValueError, KeyError, TypeError, IndexError):
                self.decode_errors += 1


def main(host: str, mode: str, seconds: float, output: str) -> None:
    tally = Tally(mode)
    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"bench-{mode}")
    topic = "openplc/#" if mode == "generic" else "spBv1.0/#"
    client.on_connect = lambda c, *_a: c.subscribe(topic, qos=0)
    client.on_message = tally.on_message
    client.max_inflight_messages_set(1000)
    client.connect(host, 1883, keepalive=30)
    client.loop_start()
    started = time.monotonic()
    time.sleep(seconds)
    client.loop_stop()
    elapsed = time.monotonic() - started
    with tally.lock:
        rates = sorted(tally.per_second.values())
        summary = {
            "mode": mode,
            "seconds": round(elapsed, 1),
            "messages": tally.messages,
            "values": tally.values,
            "values_per_s": round(tally.values / elapsed, 1),
            "messages_per_s": round(tally.messages / elapsed, 1),
            "min_messages_in_a_second": rates[1] if len(rates) > 2 else None,
            "leaves_seen": len(tally.last),
            "regressions": tally.regressions,
            "decode_errors": tally.decode_errors,
            "births": tally.births,
            "deaths": tally.deaths,
            "seq_gaps": tally.seq_gaps,
        }
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4])
