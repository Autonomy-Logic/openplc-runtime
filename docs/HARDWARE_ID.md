# Hardware ID v2

The hardware ID identifies the physical machine an OpenPLC runtime licence was bought for, so a
lost licence can be recovered from the same hardware after a wipe or reinstall. Nothing is enforced
on the device: the ID only has to be the same every time it is computed on the same hardware.

The reader is `scripts/hardware-id.sh`, plain bash with no dependency beyond coreutils. The block
between its `BEGIN hardware-id v2` and `END hardware-id v2` markers is the canonical copy: the
orchestrator-agent and autonomy-node installers embed it verbatim, and their checks compare it byte
for byte with this repository.

Version 1 (`openplc-dev-v1`, used by VPP licences and debug function code 0x48) is unchanged.

## Format

```
hw2:<64 lowercase hex characters>
```

## Derivation

1. Read each element below. Every value has NUL bytes removed, surrounding whitespace trimmed and is
   lower-cased.
2. Hash each element on its own: `sha256("openplc-hw-v2|<kind>|<value>")`, as lowercase hex.
3. Sort the element hashes, join them with a newline after each one, and hash the result:
   `ID = "hw2:" + sha256(h1 "\n" h2 "\n" ... hn "\n")`.

| Kind | Source | Accepted when | Strong |
| --- | --- | --- | --- |
| `serial` | `/proc/device-tree/serial-number` | Not empty | Yes |
| `uuid` | `/sys/class/dmi/id/product_uuid` | Hex with dashes; not all `0`, not all `f`, not `03000200-0400-0500-0006-000700080009` | Yes |
| `board` | `/sys/class/dmi/id/board_serial` | Not empty, `to be filled by o.e.m.`, `default string`, `not specified` or `0123456789` | Yes |
| `mac` | `/sys/class/net/*/address`, one element per NIC | See NIC rules; never inside a VM | Yes |
| `cpu` | `/proc/cpuinfo`, first match of each field | x86: `<vendor_id>\|<model name>`; ARM: `<CPU implementer>\|<CPU part>` | No |

NIC rules, all of which must hold:

- the interface has a `device` link (rules out bridges, veth, tun, bonds and other virtual links);
- it is not wireless (no `wireless` or `phy80211` entry);
- `addr_assign_type` is `0` (factory address);
- the address is a well-formed MAC, not all zeros, with the locally administered bit clear;
- no directory on its device path has `removable` set to `removable`;
- it is not on USB, unless a USB port on its device path reports `connect_type` = `hardwired`.

Other rules:

- **No software state.** No element comes from the OS, kernel, architecture string, hostname or
  installed packages. The same hardware gives the same ID under a 32-bit or 64-bit OS.
- **VMs.** When `/proc/cpuinfo` lists the `hypervisor` flag, or the DMI `sys_vendor` or
  `product_name` names VMware, QEMU, KVM, VirtualBox (innotek), Xen, Amazon EC2, Google Compute
  Engine, Parallels or Microsoft Corporation "Virtual Machine", NICs are left out.
- **Refused hosts.** When `/proc/sys/kernel/osrelease` or `/proc/version` mentions `microsoft`,
  `wsl` or `linuxkit` (WSL2, Docker Desktop), no ID is produced.
- **No strong element, no ID.** The CPU alone never yields an ID.
- **Frozen.** Every rule and placeholder above is part of v2. Any change after licences are issued is
  a new version, v3.

## Usage

```
scripts/hardware-id.sh             # prints the ID; exit status 2 when none can be derived
scripts/hardware-id.sh --elements  # prints "<kind> <element hash>" per element
```

`OPENPLC_HWID_ROOT` replaces `/` for every read. It exists for tests only.

## Test vectors

`tests/pytest/hardware_id/vectors.json` holds fake hardware trees with their element values and the
expected ID. The tests check the reader against them and recompute every hash independently in Python.

| Vector | Elements | ID |
| --- | --- | --- |
| `raspberry-pi` | serial, mac, cpu | `hw2:363598c3230281d8cc2cfcdd1b772a6957aa6b8fbd631712ba1b195be5d22318` |
| `x86-industrial-pc` | uuid, board, 2 mac, cpu | `hw2:5337b315679912a71e5f004a718544e1282e004f13acb0ac17c4c8142587a14c` |
| `vm` | uuid, cpu | `hw2:5e26377d20855444f09a7b40f13d8c4e00550166e98efbc6d1d01c05b51a5eb1` |
| `placeholders-only` | cpu | none |
| `wsl2` | none | none |
