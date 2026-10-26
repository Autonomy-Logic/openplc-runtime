# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Hardware ID v2 reader (scripts/hardware-id.sh) against fake hardware trees."""

import copy
import hashlib
import json
import os
import pathlib
import subprocess
from dataclasses import dataclass

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "hardware-id.sh"
VECTORS = json.loads((pathlib.Path(__file__).parent / "vectors.json").read_text())["vectors"]

Tree = dict[str, dict[str, str]]


@dataclass(frozen=True)
class Result:
    code: int
    out: str


def element_hash(kind: str, value: str) -> str:
    return hashlib.sha256(f"openplc-hw-v2|{kind}|{value}".encode()).hexdigest()


def expected_id(elements: list[list[str]]) -> str:
    hashes = sorted(element_hash(kind, value) for kind, value in elements)
    return "hw2:" + hashlib.sha256("".join(h + "\n" for h in hashes).encode()).hexdigest()


def build(root: pathlib.Path, tree: Tree) -> pathlib.Path:
    for rel, content in tree.get("files", {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for rel, target in tree.get("links", {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    return root


def run(root: pathlib.Path, *args: str) -> Result:
    env = dict(os.environ, OPENPLC_HWID_ROOT=str(root))
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )
    return Result(proc.returncode, proc.stdout)


def vector(name: str) -> Tree:
    return copy.deepcopy(next(v for v in VECTORS if v["name"] == name))


def elements_of(root: pathlib.Path) -> list[str]:
    return [line.split(" ")[0] for line in run(root, "--elements").out.splitlines()]


@pytest.mark.parametrize("vec", VECTORS, ids=[v["name"] for v in VECTORS])
def test_vector(tmp_path: pathlib.Path, vec: Tree) -> None:
    root = build(tmp_path, vec)
    result = run(root)
    if vec["id"] is None:
        assert result.code == 2
        assert result.out == ""
        return
    assert result.code == 0
    assert result.out == vec["id"] + "\n"
    assert vec["id"] == expected_id(vec["elements"])
    listed = sorted(run(root, "--elements").out.splitlines())
    assert listed == sorted(f"{k} {element_hash(k, v)}" for k, v in vec["elements"])


def test_vectors_are_listed_in_the_spec() -> None:
    spec = (REPO / "docs" / "HARDWARE_ID.md").read_text()
    for vec in VECTORS:
        assert f"`{vec['name']}`" in spec
        if vec["id"]:
            assert vec["id"] in spec


def test_same_hardware_same_id_whatever_the_software(tmp_path: pathlib.Path) -> None:
    first = build(tmp_path / "a", vector("raspberry-pi"))
    other = vector("raspberry-pi")
    other["files"]["proc/version"] = "Linux version 6.1.0-rpi7-rpi-v7l (armv7l) #1 SMP\n"
    other["files"]["etc/hostname"] = "renamed\n"
    second = build(tmp_path / "b", other)
    assert run(first).out == run(second).out


def test_reads_are_trimmed_and_lowercased(tmp_path: pathlib.Path) -> None:
    vec = vector("x86-industrial-pc")
    vec["files"]["sys/class/dmi/id/board_serial"] = "  CNWS2000900ABC \n"
    assert run(build(tmp_path, vec)).out == vector("x86-industrial-pc")["id"] + "\n"


@pytest.mark.parametrize(
    "uuid",
    [
        "00000000-0000-0000-0000-000000000000",
        "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
        "03000200-0400-0500-0006-000700080009",
        "Not Settable",
        "",
    ],
)
def test_uuid_placeholders_rejected(tmp_path: pathlib.Path, uuid: str) -> None:
    vec = vector("x86-industrial-pc")
    vec["files"]["sys/class/dmi/id/product_uuid"] = uuid + "\n"
    assert "uuid" not in elements_of(build(tmp_path, vec))


@pytest.mark.parametrize(
    "serial", ["To be filled by O.E.M.", "Default string", "Not Specified", "0123456789", ""]
)
def test_board_serial_placeholders_rejected(tmp_path: pathlib.Path, serial: str) -> None:
    vec = vector("x86-industrial-pc")
    vec["files"]["sys/class/dmi/id/board_serial"] = serial + "\n"
    assert "board" not in elements_of(build(tmp_path, vec))


def _single_nic(name: str, mac: str, aat: str, device: str | None) -> Tree:
    files = {
        "proc/cpuinfo": "vendor_id\t: GenuineIntel\nmodel name\t: Test CPU\n",
        f"sys/class/net/{name}/address": mac + "\n",
        f"sys/class/net/{name}/addr_assign_type": aat + "\n",
    }
    links: dict[str, str] = {}
    if device:
        files[f"sys/{device}/.present"] = ""
        links[f"sys/class/net/{name}/device"] = "../../../" + device
    return {"files": files, "links": links}


PCI = "devices/pci0000:00/0000:00:1c.0/0000:01:00.0"
USB = "devices/pci0000:00/0000:00:14.0/usb1/1-1/1-1:1.0"


def test_wired_pci_nic_counts(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path, _single_nic("enp1s0", "00:e0:4c:68:00:01", "0", PCI))
    assert (
        run(root).out
        == expected_id([["mac", "00:e0:4c:68:00:01"], ["cpu", "genuineintel|test cpu"]]) + "\n"
    )


def test_virtual_nic_without_device_link_ignored(tmp_path: pathlib.Path) -> None:
    assert run(build(tmp_path, _single_nic("veth0", "00:e0:4c:68:00:01", "0", None))).code == 2


def test_wireless_nic_ignored(tmp_path: pathlib.Path) -> None:
    tree = _single_nic("wlp2s0", "00:e0:4c:68:00:01", "0", PCI)
    tree["files"]["sys/class/net/wlp2s0/phy80211/.present"] = ""
    assert run(build(tmp_path, tree)).code == 2


def test_changed_mac_ignored(tmp_path: pathlib.Path) -> None:
    assert run(build(tmp_path, _single_nic("enp1s0", "00:e0:4c:68:00:01", "3", PCI))).code == 2


def test_locally_administered_mac_ignored(tmp_path: pathlib.Path) -> None:
    assert run(build(tmp_path, _single_nic("enp1s0", "02:e0:4c:68:00:01", "0", PCI))).code == 2


def test_removable_nic_ignored(tmp_path: pathlib.Path) -> None:
    tree = _single_nic("enp1s0", "00:e0:4c:68:00:01", "0", PCI)
    tree["files"]["sys/devices/pci0000:00/0000:00:1c.0/removable"] = "removable\n"
    assert run(build(tmp_path, tree)).code == 2


def test_usb_nic_ignored(tmp_path: pathlib.Path) -> None:
    tree = _single_nic("enx0", "00:24:9b:11:22:33", "0", USB)
    tree["files"]["sys/devices/pci0000:00/0000:00:14.0/usb1/1-1/port/connect_type"] = "hotplug\n"
    assert run(build(tmp_path, tree)).code == 2


def test_hardwired_usb_nic_counts(tmp_path: pathlib.Path) -> None:
    tree = _single_nic("eth0", "b8:27:eb:11:22:33", "0", USB)
    tree["files"]["sys/devices/pci0000:00/0000:00:14.0/usb1/1-1/port/connect_type"] = "hardwired\n"
    assert elements_of(build(tmp_path, tree)) == ["mac", "cpu"]


def test_plugging_a_usb_nic_keeps_the_id(tmp_path: pathlib.Path) -> None:
    before = run(build(tmp_path / "a", vector("raspberry-pi"))).out
    vec = vector("raspberry-pi")
    vec["files"]["sys/class/net/eth1/address"] = "00:24:9b:aa:bb:cc\n"
    vec["files"]["sys/class/net/eth1/addr_assign_type"] = "0\n"
    vec["files"]["sys/devices/platform/scb/fd500000.pcie/usb1/1-1/1-1:1.0/.present"] = ""
    vec["links"][
        "sys/class/net/eth1/device"
    ] = "../../../devices/platform/scb/fd500000.pcie/usb1/1-1/1-1:1.0"
    assert run(build(tmp_path / "b", vec)).out == before


def test_vm_detected_by_dmi_leaves_nics_out(tmp_path: pathlib.Path) -> None:
    tree = _single_nic("ens3", "00:16:3e:11:22:33", "0", PCI)
    tree["files"]["sys/class/dmi/id/sys_vendor"] = "VMware, Inc.\n"
    assert run(build(tmp_path, tree)).code == 2


def test_vm_detected_by_hyperv_product_leaves_nics_out(tmp_path: pathlib.Path) -> None:
    tree = _single_nic("eth0", "00:15:5d:11:22:33", "0", PCI)
    tree["files"]["sys/class/dmi/id/sys_vendor"] = "Microsoft Corporation\n"
    tree["files"]["sys/class/dmi/id/product_name"] = "Virtual Machine\n"
    assert run(build(tmp_path, tree)).code == 2


def test_microsoft_hardware_is_not_a_vm(tmp_path: pathlib.Path) -> None:
    tree = _single_nic("eth0", "00:15:5d:11:22:33", "0", PCI)
    tree["files"]["sys/class/dmi/id/sys_vendor"] = "Microsoft Corporation\n"
    tree["files"]["sys/class/dmi/id/product_name"] = "Surface Pro 9\n"
    assert run(build(tmp_path, tree)).code == 0


def test_vm_detected_by_hypervisor_flag(tmp_path: pathlib.Path) -> None:
    tree = _single_nic("ens3", "00:16:3e:11:22:33", "0", PCI)
    tree["files"]["proc/cpuinfo"] = "vendor_id\t: GenuineIntel\nflags\t\t: fpu sse hypervisor\n"
    assert run(build(tmp_path, tree)).code == 2


@pytest.mark.parametrize(
    "path,content",
    [
        ("proc/sys/kernel/osrelease", "5.15.153.1-microsoft-standard-WSL2\n"),
        ("proc/version", "Linux version 6.10.14-linuxkit (root@buildkitsandbox)\n"),
    ],
)
def test_refused_hosts_produce_nothing(tmp_path: pathlib.Path, path: str, content: str) -> None:
    vec = vector("x86-industrial-pc")
    vec["files"][path] = content
    root = build(tmp_path, vec)
    assert run(root) == Result(2, "")
    assert run(root, "--elements") == Result(2, "")


def test_cpu_alone_gives_no_id(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path, {"files": {"proc/cpuinfo": "vendor_id\t: GenuineIntel\n"}})
    assert run(root).code == 2


def test_empty_root_gives_no_id(tmp_path: pathlib.Path) -> None:
    assert run(tmp_path) == Result(2, "")


def test_arm_cpu_uses_first_core_registers(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path, vector("raspberry-pi"))
    cpu = [line for line in run(root, "--elements").out.splitlines() if line.startswith("cpu ")]
    assert cpu == [f"cpu {element_hash('cpu', '0x41|0xd08')}"]


def _block(path: pathlib.Path) -> str:
    text = path.read_text()
    start = text.index("# BEGIN hardware-id v2")
    end = text.index("# END hardware-id v2") + len("# END hardware-id v2\n")
    return text[start:end]


def test_docker_installer_embeds_the_canonical_block() -> None:
    assert _block(REPO / "scripts" / "install-docker.sh") == _block(SCRIPT)


def test_record_is_the_id_then_the_elements(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path, vector("raspberry-pi"))
    lines = run(root, "--record").out.splitlines()
    assert lines[0] == vector("raspberry-pi")["id"]
    assert lines[1:] == run(root, "--elements").out.splitlines()


def _store(root: pathlib.Path, target: pathlib.Path) -> int:
    """Calls hw2_store from a shell with the installers' strict options."""
    script = f'set -euo pipefail; source "{SCRIPT}"; hw2_store "{target}"'
    env = dict(os.environ, OPENPLC_HWID_ROOT=str(root))
    return subprocess.run(["bash", "-c", script], env=env, check=False).returncode


def test_store_writes_the_record_under_strict_mode(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path / "root", vector("x86-industrial-pc"))
    target = tmp_path / "hardware-id"
    assert _store(root, target) == 0
    assert target.read_text() == run(root, "--record").out
    assert oct(target.stat().st_mode & 0o777) == "0o644"


def test_store_never_replaces_a_record(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path / "root", vector("x86-industrial-pc"))
    target = tmp_path / "hardware-id"
    target.write_text("hw2:" + "0" * 64 + "\n")
    assert _store(root, target) == 0
    assert target.read_text() == "hw2:" + "0" * 64 + "\n"


def test_store_leaves_nothing_without_an_id(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "hardware-id"
    assert _store(tmp_path / "empty", target) == 1
    assert list(tmp_path.iterdir()) == []


def test_store_cli(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path / "root", vector("raspberry-pi"))
    target = tmp_path / "hardware-id"
    assert run(root, "--store", str(target)).code == 0
    assert target.read_text().splitlines()[0] == vector("raspberry-pi")["id"]
