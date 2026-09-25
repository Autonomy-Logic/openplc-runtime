# EtherCAT

The EtherCAT master is [EtherDOG](https://github.com/Autonomy-Logic/EtherDOG), a separate process the webserver starts and supervises. The runtime reaches it only through its protocol (EtherDOG's `docs/PROTOCOL.md`).

```
Editor upload ──► webserver ──► EtherDOG ◄── wire ──► EtherCAT slaves
                     │   ethercat_busconfig.json     ▲
                     │                                │ datagrams, one frame each way per bus cycle
                     └──► plc_main (ethercat plugin) ─┘
                          ethercat_iomapping.json
```

## Configuration files

The Editor (runtime v4.3.0 or newer) writes two files into the upload's `conf/` folder:

| File | Read by | Contents |
|---|---|---|
| `ethercat_busconfig.json` | EtherDOG | Masters, slaves, PDOs, SDOs, channels, and `master.cycle_time_us` |
| `ethercat_iomapping.json` | `plc_main`'s `ethercat` plugin | `(master name, slave, index, subindex)` to IEC location |

```json
{
  "version": 1,
  "masters": [
    {
      "name": "EK_BUS",
      "entries": [
        { "slave": 1, "index": "0x6000", "subindex": 1, "iec_location": "%IX0.0" }
      ]
    }
  ]
}
```

An upload whose single `conf/ethercat.json` (older Editors) describes masters is rejected before anything on the device changes.

## Components

| Piece | Where |
|---|---|
| Supervisor: start, restart, token, session file, busconfig delivery, discovery commands | `webserver/etherdog_manager.py` |
| Discovery and status routes (`/api/discovery/ethercat/*`) | `webserver/discovery/discovery_routes.py` |
| Client plugin: layout join, relay thread, reconnect | `core/src/drivers/plugins/native/ethercat/` |
| Build | `install.sh` `build_etherdog` puts the binary at `build/etherdog` |

The webserver writes `/run/runtime/etherdog.json` (mode 0600), with the control endpoint, the per-boot token and the data transport. The plugin reads it on each connect. EtherDOG's log lines go to `log_runtime.socket`, so they show up in the runtime log with an `[ETHERDOG]` prefix.

## Timing

`master.cycle_time_us` from the busconfig sets the bus cycle. The relay thread in `plc_main` has no timer of its own:
1. It wakes on each input frame and publishes `%I` through the journal.
2. It reads `%Q` under `image_lock`.
3. It answers with one output frame.

So the runtime exchanges data with EtherDOG once per bus cycle. On an SLM-RP4 with an EK1818, 1000 us and 2000 us both measured at the configured rate, on the wire and in the relay.

## Failure behaviour

- **EtherDOG exits:** the webserver restarts it with backoff and loads the busconfig again. The plugin notices the silence within 1 s and reconnects without restarting `plc_main`. Meanwhile inputs keep their last values.
- **The client goes quiet for 100 ms:** EtherDOG drives the outputs to zero.
- **A mapping entry has no matching PDO entry, or the direction or width differs:** the plugin fails to start and names the entry.

## Building EtherDOG

`install.sh` takes the first of these that exists:
1. `$ETHERDOG_SRC`
2. a checkout at `./etherdog`
3. a clone of `$ETHERDOG_REPO` at `$ETHERDOG_REF`, into `third_party/etherdog`

The source stays in the install. EtherDOG needs CMake 3.28 or newer, which `install.sh` already provides.
