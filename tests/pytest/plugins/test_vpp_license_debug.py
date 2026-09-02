"""Tests for the webserver-level VPP license debug FCs (0x48/0x49/0x4A).

Covers the raw-PDU responses the editor's modbus-pdu.ts parsers expect, the raw
(non-hex-decoded) anchor semantics that must match the .so (D70d), and the 0x49
write / 0x4A read round-trip landing on the .license sibling of the plugin
config (same path the bundle + the .so use).

Blob integrity is exercised against the REAL signed golden blob rather than a
made-up 98 bytes: see ``_GOLDEN_BLOB_HEX``.
"""
import logging
import os
import zlib

import pytest

lic = pytest.importorskip(
    "webserver.vpp_license_debug",
    reason="runtime webserver package not importable (no venv)",
)


def _hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


# The real signed 98-byte license blob, copied verbatim from
# openplc-packages/license-core/test/license-golden-signed.json ("blobHex") --
# the same vector license_core's host test and the editor/backend unit tests use
# (anchor 00b18ced -> deviceId 659a3520540f803625ddc34081e893d3, product
# 29a17c7c2486d355). Using it here means these tests assert the runtime against
# an artifact produced by ANOTHER implementation, not against bytes this file
# made up: if the magic or the crc32 range ever drifts on either side, this
# literal stops validating.
_GOLDEN_BLOB_HEX = (
    "4f504c430100659a3520540f803625ddc34081e893d329a17c7c2486d355"
    "fbff79f73b679ce59fa93304507867e82d7b41b93acd98274dc48531299e"
    "2215b1ae72881573bd800ba73a5c7731d13224772c90df806051d42d78b5"
    "c97a046a2ae3a72d"
)


def _golden_blob() -> bytes:
    blob = bytes.fromhex(_GOLDEN_BLOB_HEX)
    assert len(blob) == 98
    return blob


def _blob_with_bad_crc() -> bytes:
    """The golden blob with one payload byte flipped and the stored crc left
    alone: right size, right magic, crc no longer covers the content. This is
    what a torn write or a flipped flash cell produces."""
    blob = bytearray(_golden_blob())
    blob[40] ^= 0xFF
    assert zlib.crc32(bytes(blob[:94])) != int.from_bytes(blob[94:98], "little")
    return bytes(blob)


def _blob_without_magic() -> bytes:
    """98 bytes whose first 4 are not `4F 50 4C 43`, with the crc recomputed so
    that ONLY the magic is wrong -- otherwise this case would be
    indistinguishable from the bad-crc one."""
    blob = bytearray(_golden_blob())
    blob[0:4] = b"\x00\x00\x00\x00"
    blob[94:98] = zlib.crc32(bytes(blob[:94])).to_bytes(4, "little")
    return bytes(blob)


def _install_plugin(tmp_path, monkeypatch):
    """Fake one installed VPP plugin whose config_path lives under a temp cwd."""
    cwd = tmp_path / "runtime"
    (cwd / "build" / "vpp").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    (cwd / "vpp_plugins.conf").write_text("dummy\n")
    config_path = str(cwd / "build" / "vpp" / "rpi_gpio.json")

    class _P:
        name = "rpi_gpio"
        config_path = None

        def __init__(self, cp):
            self.config_path = cp

    class _Conf:
        plugins = [_P(config_path)]

    monkeypatch.setattr(lic.PluginsConfiguration, "from_file", classmethod(lambda cls, _p: _Conf()))
    return config_path


def test_is_license_command():
    assert lic.is_license_command("48")
    assert lic.is_license_command("49 00 62")
    assert lic.is_license_command("4A")
    assert not lic.is_license_command("41 00 00")
    assert not lic.is_license_command("")


def test_get_board_id_returns_raw_ascii_anchor(tmp_path, monkeypatch):
    # Mimic /proc/device-tree/serial-number: ASCII hex + trailing NUL.
    anchor_file = tmp_path / "serial-number"
    anchor_file.write_bytes(b"8625807b0a83ae7d\x00")
    monkeypatch.setattr(lic, "ANCHOR_PATH", str(anchor_file))

    resp = lic.handle_license_command("48")
    parts = resp.split()
    assert parts[0] == "48"
    assert parts[1] == "7E"  # SUCCESS
    assert parts[2] == "10"  # 16 bytes
    body = bytes(int(p, 16) for p in parts[3:])
    # RAW ascii, NUL stripped -- NOT hex-decoded (the .so hashes exactly these bytes).
    assert body == b"8625807b0a83ae7d"


def test_get_board_id_missing_anchor_is_empty_success(tmp_path, monkeypatch):
    # No anchor -> LIC_UNSUPPORTED (review 2026-08-20, R2): on this medium 0x48
    # is ONLY the licensing anchor, and SUCCESS/0 made every anchor-less host
    # derive the SAME deviceId -- a purchase bound to it never validated.
    monkeypatch.setattr(lic, "ANCHOR_PATH", str(tmp_path / "nope"))
    assert lic.handle_license_command("48") == "48 85"


def test_write_refuses_path_traversal(tmp_path, monkeypatch):
    # A forged vpp_plugins.conf whose config_path escapes the runtime root must
    # NOT let 0x49 write outside it (defense-in-depth; mirrors apply_vpp_plugin_conf).
    #
    # The blob is the VALID golden one on purpose: with blob validation in front
    # of the path resolution, an invalid blob would be refused before the guard
    # was ever reached, and this test could no longer tell a working guard from a
    # missing one.
    cwd = tmp_path / "runtime"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    (cwd / "vpp_plugins.conf").write_text("dummy\n")
    escaping = str(tmp_path / "outside" / "evil.json")  # sibling of cwd -> escapes root

    class _P:
        name = "x"
        config_path = escaping

    class _Conf:
        plugins = [_P()]

    monkeypatch.setattr(lic.PluginsConfiguration, "from_file", classmethod(lambda cls, _p: _Conf()))

    cmd = _hex(bytes([0x49, 0x00, 0x62]) + _golden_blob())
    assert lic.handle_license_command(cmd) == "49 85"  # refused -> LIC_UNSUPPORTED
    assert not os.path.exists(tmp_path / "outside" / "evil.license")


def test_read_license_empty_when_no_conf(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no vpp_plugins.conf here
    assert lic.handle_license_command("4A") == "4A 83"  # LIC_EMPTY


def test_write_then_read_roundtrip(tmp_path, monkeypatch):
    """A blob that really validates round-trips as SUCCESS.

    This test used to hand 0x49 `magic + bytes(range(1,95))` -- 98 bytes with a
    deliberately WRONG crc32 -- and assert SUCCESS on the read back. It pinned
    the very defect that made a Pi with an unverifiable license report "Licensed"
    to the editor: it locked the bug in place instead of catching it. The blob
    here is the real signed golden one, so SUCCESS now means what it says.
    """
    config_path = _install_plugin(tmp_path, monkeypatch)
    blob = _golden_blob()

    cmd = _hex(bytes([0x49, 0x00, 0x62]) + blob)  # [0x49][len=98 u16BE][blob]
    assert lic.handle_license_command(cmd) == "49 7E"  # SUCCESS

    expected_path = config_path[:-5] + ".license"
    assert os.path.exists(expected_path)
    assert os.path.getsize(expected_path) == 98

    read = lic.handle_license_command("4A")
    parts = read.split()
    assert parts[0] == "4A" and parts[1] == "7E"
    assert parts[2] == "00" and parts[3] == "62"  # len 98, u16BE
    assert bytes(int(p, 16) for p in parts[4:]) == blob


def test_write_wrong_size_is_corrupt(tmp_path, monkeypatch):
    _install_plugin(tmp_path, monkeypatch)
    cmd = _hex(bytes([0x49, 0x00, 0x04]) + b"\x01\x02\x03\x04")  # not 98
    assert lic.handle_license_command(cmd) == "49 84"  # LIC_CORRUPT


def test_write_without_installed_plugin_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no vpp_plugins.conf
    # Valid blob, so UNSUPPORTED can only come from the missing plugin config.
    cmd = _hex(bytes([0x49, 0x00, 0x62]) + _golden_blob())
    assert lic.handle_license_command(cmd) == "49 85"  # LIC_UNSUPPORTED


# --------------------------------------------------------------------------
# Blob integrity, both directions
#
# 0x4A used to test the LENGTH only. A 98-byte file that does not verify -- an
# SD card cloned from another Pi, corrupted flash, a torn write -- answered
# `4A 7E`, which the editor reads as "magic + crc32 verified", so it reported
# "Licensed" and returned BEFORE asking the backend for a fresh license. The one
# automatic repair path never ran precisely because the editor trusted the blob,
# while license_core refused it and the plugin dropped to demo. The same file on
# an ESP32 answers 0x83/0x84 and the editor recovers automatically.
#
# 0x49 wrote whatever it was handed, so 98 bytes of junk destroyed a valid
# license and answered SUCCESS.
# --------------------------------------------------------------------------


def test_read_reports_corrupt_when_the_crc_does_not_verify(tmp_path, monkeypatch):
    config_path = _install_plugin(tmp_path, monkeypatch)
    with open(config_path[:-5] + ".license", "wb") as handle:
        handle.write(_blob_with_bad_crc())

    assert lic.handle_license_command("4A") == "4A 84"  # LIC_CORRUPT, not SUCCESS


def test_read_reports_empty_when_the_magic_is_absent(tmp_path, monkeypatch):
    config_path = _install_plugin(tmp_path, monkeypatch)
    with open(config_path[:-5] + ".license", "wb") as handle:
        handle.write(_blob_without_magic())

    assert lic.handle_license_command("4A") == "4A 83"  # LIC_EMPTY


def test_read_reports_empty_for_a_zeroed_license_file(tmp_path, monkeypatch):
    """98 zero bytes is what a wiped or freshly-truncated file looks like; the
    old length-only check answered `4A 7E 00 62` + 98 zeros."""
    config_path = _install_plugin(tmp_path, monkeypatch)
    with open(config_path[:-5] + ".license", "wb") as handle:
        handle.write(bytes(98))

    assert lic.handle_license_command("4A") == "4A 83"  # LIC_EMPTY (no magic)


def test_read_reports_corrupt_for_a_wrong_sized_license_file(tmp_path, monkeypatch):
    config_path = _install_plugin(tmp_path, monkeypatch)
    with open(config_path[:-5] + ".license", "wb") as handle:
        handle.write(_golden_blob()[:97])

    assert lic.handle_license_command("4A") == "4A 84"  # LIC_CORRUPT


def test_write_refuses_a_blob_whose_crc_does_not_verify(tmp_path, monkeypatch):
    _install_plugin(tmp_path, monkeypatch)
    cmd = _hex(bytes([0x49, 0x00, 0x62]) + _blob_with_bad_crc())
    assert lic.handle_license_command(cmd) == "49 84"  # LIC_CORRUPT


def test_write_refuses_a_blob_without_the_magic(tmp_path, monkeypatch):
    _install_plugin(tmp_path, monkeypatch)
    cmd = _hex(bytes([0x49, 0x00, 0x62]) + _blob_without_magic())
    assert lic.handle_license_command(cmd) == "49 83"  # LIC_EMPTY


def test_a_refused_write_leaves_the_previous_license_untouched(tmp_path, monkeypatch):
    """The destructive half: junk sent to 0x49 must not overwrite a good license.

    Observed before this change: a valid license followed by 98 bytes of junk
    answered `49 7E` and the file became 98 zeros -- a remote, persistent
    downgrade of a licensed device to demo.
    """
    config_path = _install_plugin(tmp_path, monkeypatch)
    license_path = config_path[:-5] + ".license"
    good = _golden_blob()
    assert lic.handle_license_command(_hex(bytes([0x49, 0x00, 0x62]) + good)) == "49 7E"

    junk = bytes(98)
    assert lic.handle_license_command(_hex(bytes([0x49, 0x00, 0x62]) + junk)) == "49 83"

    with open(license_path, "rb") as handle:
        assert handle.read() == good
    assert lic.handle_license_command("4A").startswith("4A 7E 00 62")


def test_non_license_fc_passes_through():
    assert lic.handle_license_command("41 00 00 00 01") is None


# --------------------------------------------------------------------------
# Atomicity and I/O failures
# --------------------------------------------------------------------------


def test_a_failed_rename_leaves_the_previous_license_intact_and_no_debris(tmp_path, monkeypatch):
    """The torn-write case: the write fails AFTER the new bytes are on disk.

    `open(path, "wb")` truncates before writing, so ENOSPC / a power cut / the
    process dying mid-activation destroyed the PREVIOUS, VALID license and left
    the device in demo on the next start. With tmp + replace the old license is
    still the one at `path`, and the temporary is cleaned up.
    """
    config_path = _install_plugin(tmp_path, monkeypatch)
    license_path = config_path[:-5] + ".license"
    good = _golden_blob()
    assert lic.handle_license_command(_hex(bytes([0x49, 0x00, 0x62]) + good)) == "49 7E"

    def _boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(lic.os, "replace", _boom)

    other = bytearray(good)
    other[6] ^= 0x01  # a different, still-valid-looking device_id
    other[94:98] = zlib.crc32(bytes(other[:94])).to_bytes(4, "little")
    resp = lic.handle_license_command(_hex(bytes([0x49, 0x00, 0x62]) + bytes(other)))

    assert resp == "49 82"  # IO_ERROR, not an exception and not SUCCESS
    with open(license_path, "rb") as handle:
        assert handle.read() == good
    leftovers = [p for p in os.listdir(os.path.dirname(license_path)) if p.endswith(".tmp")]
    assert leftovers == []


def test_write_maps_a_failing_filesystem_to_io_error(tmp_path, monkeypatch):
    """A read-only SD card is the number-one failure mode of an industrial Pi.

    It used to propagate PermissionError out of handle_license_command -- against
    that function's own "never raises for a well-formed license FC" contract --
    and the WebSocket layer flattened it into `{"success": false, "error": ...}`,
    a shape the editor's PDU parser never sees as a status byte. Bare metal
    answers 0x82 for exactly this.
    """
    _install_plugin(tmp_path, monkeypatch)

    def _read_only(*_args, **_kwargs):
        raise PermissionError(13, "Read-only file system")

    monkeypatch.setattr(lic.tempfile, "mkstemp", _read_only)

    cmd = _hex(bytes([0x49, 0x00, 0x62]) + _golden_blob())
    assert lic.handle_license_command(cmd) == "49 82"  # IO_ERROR


def test_read_maps_an_unreadable_license_to_io_error(tmp_path, monkeypatch):
    config_path = _install_plugin(tmp_path, monkeypatch)
    license_path = config_path[:-5] + ".license"
    with open(license_path, "wb") as handle:
        handle.write(_golden_blob())

    real_open = open

    def _fake_open(path, *args, **kwargs):
        if str(path) == license_path:
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)

    assert lic.handle_license_command("4A") == "4A 82"  # IO_ERROR


# --------------------------------------------------------------------------
# Anchor normalization (0x48)
#
# The C is canonical: rpi_plugin.c is the side that decides whether the license
# verifies. Byte-for-byte parity with the real C source is pinned separately, by
# test_vpp_anchor_cross_language.py; these are the wire-level consequences.
# --------------------------------------------------------------------------


def test_anchor_keeps_a_trailing_tab(tmp_path, monkeypatch):
    """TAB is NOT in the C's strip list, so it must not be in ours either.

    An anchor ending in 0x09 stripped here but not there derives a different
    device_id on each side: sha256("openplc-dev-v1|" + "8625807b0a83ae7d\\t")[:16]
    is ac07623afa23c771..., while the .so computes 7146518f9842adac... The
    purchased license would simply never work, with nothing in any log.
    """
    anchor_file = tmp_path / "serial-number"
    anchor_file.write_bytes(b"8625807b0a83ae7d\t\x00")
    monkeypatch.setattr(lic, "ANCHOR_PATH", str(anchor_file))

    parts = lic.handle_license_command("48").split()
    assert parts[:2] == ["48", "7E"]
    assert parts[2] == "11"  # 17 bytes: the TAB is still there
    assert bytes(int(p, 16) for p in parts[3:]) == b"8625807b0a83ae7d\t"


def test_anchor_strip_set_is_exactly_the_four_bytes_the_c_strips():
    assert lic.ANCHOR_STRIP_BYTES == b"\x00\r\n "
    assert b"\t" not in lic.ANCHOR_STRIP_BYTES


def test_anchor_at_the_ceiling_is_served(tmp_path, monkeypatch):
    anchor_file = tmp_path / "serial-number"
    anchor_file.write_bytes(b"a" * 64 + b"\x00")
    monkeypatch.setattr(lic, "ANCHOR_PATH", str(anchor_file))

    parts = lic.handle_license_command("48").split()
    assert parts[:3] == ["48", "7E", "40"]  # 0x40 == 64
    assert bytes(int(p, 16) for p in parts[3:]) == b"a" * 64


def test_anchor_over_the_ceiling_is_refused_not_truncated(tmp_path, monkeypatch):
    """rpi_plugin.c reads `uint8_t anchor[64]`, so the verifier never sees more.

    Sending 65+ bytes on the wire would have the editor hash all of them and the
    .so hash the first 64 -> DEVICE_MISMATCH -> demo, with a license bought
    against an identity that can never validate. Refuse instead; never truncate
    silently, in either direction.
    """
    anchor_file = tmp_path / "serial-number"
    anchor_file.write_bytes(b"b" * 65)
    monkeypatch.setattr(lic, "ANCHOR_PATH", str(anchor_file))

    assert lic.handle_license_command("48") == "48 81"  # TOO_LARGE, no id bytes


def test_anchor_padding_past_the_ceiling_still_serves_the_stripped_value(tmp_path, monkeypatch):
    """A file longer than 64 bytes whose tail is all strippable is NOT an error:
    the C reads 64 bytes and strips the padding out of them, landing on exactly
    the same value this side computes from the whole file."""
    anchor_file = tmp_path / "serial-number"
    anchor_file.write_bytes(b"c" * 20 + b"\x00" * 60)
    monkeypatch.setattr(lic, "ANCHOR_PATH", str(anchor_file))

    parts = lic.handle_license_command("48").split()
    assert parts[:3] == ["48", "7E", "14"]  # 20 bytes
    assert bytes(int(p, 16) for p in parts[3:]) == b"c" * 20


# --------------------------------------------------------------------------
# Multi-plugin: documented behaviour + a trace in the log
# --------------------------------------------------------------------------


def test_more_than_one_candidate_writes_the_first_and_warns(tmp_path, monkeypatch):
    """0x49 acts on the FIRST plugin carrying a config_path.

    `candidates` filters on having a config_path, NOT on being licensable, so a
    single licensable VPP is enough for this to bite: one free VPP ahead of it in
    vpp_plugins.conf and the license lands on the free plugin's sibling, while
    0x4A reads that same wrong file back and reports SUCCESS. Disambiguating for
    real is out of scope by decision; a WARN is what makes the day it bites
    leave a trace.
    """
    cwd = tmp_path / "runtime"
    (cwd / "build" / "vpp").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    (cwd / "vpp_plugins.conf").write_text("dummy\n")
    free_config = str(cwd / "build" / "vpp" / "free_gpio.json")
    paid_config = str(cwd / "build" / "vpp" / "paid_rpi.json")

    class _P:
        def __init__(self, name, cp):
            self.name = name
            self.config_path = cp

    class _Conf:
        plugins = [_P("free_gpio", free_config), _P("paid_rpi", paid_config)]

    monkeypatch.setattr(lic.PluginsConfiguration, "from_file", classmethod(lambda cls, _p: _Conf()))

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    lic.logger.addHandler(handler)
    try:
        cmd = _hex(bytes([0x49, 0x00, 0x62]) + _golden_blob())
        assert lic.handle_license_command(cmd) == "49 7E"
    finally:
        lic.logger.removeHandler(handler)

    # Documented behaviour: first candidate wins, the second gets nothing.
    assert os.path.exists(free_config[:-5] + ".license")
    assert not os.path.exists(paid_config[:-5] + ".license")
    # ...and it is not silent.
    warnings = [r for r in records if r.levelno >= logging.WARNING]
    assert warnings, "more than one candidate must WARN"
    assert "free_gpio" in warnings[0].getMessage()


def test_a_single_candidate_does_not_warn(tmp_path, monkeypatch):
    _install_plugin(tmp_path, monkeypatch)

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    lic.logger.addHandler(handler)
    try:
        cmd = _hex(bytes([0x49, 0x00, 0x62]) + _golden_blob())
        assert lic.handle_license_command(cmd) == "49 7E"
    finally:
        lic.logger.removeHandler(handler)

    assert [r for r in records if r.levelno >= logging.WARNING] == []


# --------------------------------------------------------------------------
# Containment guard (is_inside_root)
#
# The pre-existing traversal test above uses a sibling that shares NO string
# prefix with the root, so the old buggy `startswith(root)` check rejected it
# too -- it could not tell the fixed guard from the broken one. These pin the
# two cases that actually distinguish them.
# --------------------------------------------------------------------------


def test_rejects_sibling_that_shares_the_root_as_a_string_prefix(tmp_path):
    """`/opt/runtime-evil/x` must not pass a root of `/opt/runtime`.

    This is the exact input the bare `.startswith(root)` guard accepted.
    """
    root = tmp_path / "runtime"
    root.mkdir()
    (tmp_path / "runtime-evil").mkdir()
    escaping = str(tmp_path / "runtime-evil" / "payload.json")

    assert lic.is_inside_root(escaping, str(root)) is False
    # ...and the string-prefix check it replaced would have said yes:
    assert os.path.abspath(escaping).startswith(os.path.abspath(str(root)))


def test_rejects_a_path_that_escapes_through_a_symlink(tmp_path):
    """A link out of the tree beats a lexical abspath check.

    `abspath` normalises text only; a path whose parent is a symlink pointing
    outside the root resolves outside it while still *looking* contained.
    """
    root = tmp_path / "runtime"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = root / "build"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")

    target = str(link / "payload.json")

    assert lic.is_inside_root(target, str(root)) is False
    # The lexical check this replaced sees a path squarely inside the root:
    assert os.path.abspath(target).startswith(os.path.abspath(str(root)) + os.sep)


def test_accepts_a_nested_path_that_does_not_exist_yet(tmp_path):
    """A `.license` is written before it exists; containment must still hold."""
    root = tmp_path / "runtime"
    (root / "core").mkdir(parents=True)

    assert lic.is_inside_root(str(root / "core" / "vpp.license"), str(root)) is True


def test_a_filesystem_root_does_not_refuse_everything(tmp_path):
    """Guards against the `"/" + os.sep == "//"` degenerate case.

    A separator-anchored prefix check refuses every path when the root is `/`,
    silently breaking all license writes for a process whose cwd is `/`.
    """
    root = os.path.abspath(os.sep)

    assert lic.is_inside_root(str(tmp_path / "anything.license"), root) is True


def test_resolve_license_path_returns_none_for_a_prefix_sibling(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    (tmp_path / "runtime-evil").mkdir()

    escaping = str(tmp_path / "runtime-evil" / "plugin.json")

    assert lic.resolve_license_path(escaping, str(root)) is None
    assert lic.resolve_license_path(str(root / "plugin.json"), str(root)) == str(root / "plugin.license")


def test_resolve_license_path_accepts_the_persistent_dir(tmp_path, monkeypatch):
    """The widened guard accepts config.VPP_DATA_DIR as a SECOND root -- the
    location apply_vpp_plugin_conf relocates configs to so a license survives a
    runtime update -- while still refusing anything outside both roots."""
    persist = tmp_path / "data" / "vpp"
    persist.mkdir(parents=True)
    monkeypatch.setattr(lic.config, "VPP_DATA_DIR", persist)
    root = tmp_path / "runtime"
    root.mkdir()

    # A config_path under the persistent dir resolves, even though it is NOT
    # under the runtime root passed in.
    assert lic.resolve_license_path(str(persist / "rpi.json"), str(root)) == str(persist / "rpi.license")
    # Still fails closed for a path outside BOTH roots.
    assert lic.resolve_license_path(str(tmp_path / "elsewhere" / "x.json"), str(root)) is None


def test_write_then_read_roundtrip_in_persistent_dir(tmp_path, monkeypatch):
    """0x49/0x4A round-trip when config_path points at the persistent dir (as it
    does after apply relocates it). The .license lands OUTSIDE the runtime cwd,
    which is exactly what lets it survive a build/ wipe on the next update."""
    persist = tmp_path / "data" / "vpp"
    persist.mkdir(parents=True)
    monkeypatch.setattr(lic.config, "VPP_DATA_DIR", persist)

    cwd = tmp_path / "runtime"
    cwd.mkdir()
    monkeypatch.chdir(cwd)  # cwd != persist on purpose
    (cwd / "vpp_plugins.conf").write_text("dummy\n")
    config_path = str(persist / "rpi_gpio.json")

    class _P:
        name = "rpi_gpio"

        def __init__(self, cp):
            self.config_path = cp

    class _Conf:
        plugins = [_P(config_path)]

    monkeypatch.setattr(lic.PluginsConfiguration, "from_file", classmethod(lambda cls, _p: _Conf()))

    blob = _golden_blob()
    assert lic.handle_license_command(_hex(bytes([0x49, 0x00, 0x62]) + blob)) == "49 7E"
    # Landed in the persistent dir, not under cwd/build/vpp.
    assert os.path.exists(persist / "rpi_gpio.license")
    assert not os.path.exists(cwd / "build" / "vpp" / "rpi_gpio.license")

    read = lic.handle_license_command("4A")
    assert read.startswith("4A 7E 00 62")
    assert bytes(int(p, 16) for p in read.split()[4:]) == blob
