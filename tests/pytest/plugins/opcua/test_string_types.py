"""
STRING / WSTRING support on the OPC-UA plugin.

These types read and write as nothing at all before this suite existed:
`_ctype_for()` returned None for both — on the stale grounds that "Phase 4a in
debug_dispatch.hpp explicitly stubs string reads" — so `debug_read_value`
returned None and `debug_write_value` returned False without ever calling the
runtime. strucpp has wired read_string / write_string / read_wstring /
write_wstring into type_ops[] at tags 19/20 for some time.

The failure was invisible rather than loud: a read substituted a default and
stamped it Good, so a client could not tell "the string is empty" from "this
server cannot read strings", and a write was answered Good after being dropped.

Covered here:
- the [count][payload] wire form in both directions, which is what strucpp's
  validate_payload accepts and what read_string emits
- the 126-unit cap, truncated on a CHARACTER boundary so a multi-byte UTF-8
  sequence is never split
- WSTRING as UTF-16LE code units, counted in units and not bytes

Not covered here: the StatusCode behaviour in `synchronization.py` (a failed
read reported Bad rather than a default stamped Good). It needs the asyncua
callback plumbing rather than the wire codec, and lives in
`test_read_status.py`.
"""

import pytest

# sys.path is conftest.py's job (it inserts the plugin, opcua and shared
# directories for every suite in this package); repeating it here meant two
# places to fix when the layout moves.

from asyncua import ua

from opcua_memory import (
    DEBUG_STRING_CAP,
    _decode_string,
    _encode_string,
    _is_string,
    debug_read_value,
    debug_write_value,
)
from opcua_utils import map_plc_to_opcua_type


class FakeArgs:
    """Stands in for plugin_runtime_args_t: holds one leaf's bytes."""

    def __init__(self, payload: bytes = b"", width: int = 127, status: int = 0x7E):
        self.buf = bytearray(payload.ljust(width, b"\x00"))
        self.width = width
        self.status = status
        self.written = None

    def debug_read(self, arr, elem, dest):
        for i, b in enumerate(self.buf):
            dest[i] = b
        return self.width

    def debug_write(self, arr, elem, src, length):
        self.written = bytes(bytearray(src[: int(getattr(length, "value", length))]))
        return self.status


class TestWireFormat:
    def test_string_is_recognised(self):
        assert _is_string("STRING") and _is_string("wstring")
        assert not _is_string("INT") and not _is_string("")

    def test_decode_reads_the_count_byte_not_the_padding(self):
        # read_string emits [count][payload] then pads with NULs to 127.
        wire = bytes([5]) + b"hello" + b"\x00" * 121
        buf = bytearray(wire)
        assert _decode_string("STRING", buf, 127) == "hello"

    def test_decode_empty_is_a_value_not_a_failure(self):
        buf = bytearray(b"\x00" * 127)
        assert _decode_string("STRING", buf, 127) == ""

    def test_decode_wstring_yields_utf16le_bytes(self):
        payload = "hi".encode("utf-16-le")
        buf = bytearray(bytes([2]) + payload + b"\x00" * 248)
        assert _decode_string("WSTRING", buf, 253) == payload

    def test_encode_emits_count_then_payload(self):
        assert _encode_string("STRING", "hello") == bytes([5]) + b"hello"

    def test_encode_wstring_counts_units_not_bytes(self):
        enc = _encode_string("WSTRING", "hi")
        assert enc[0] == 2           # code units
        assert len(enc) == 1 + 4     # ... but 4 payload bytes

    def test_round_trip(self):
        for value in ("", "hello", "ünïcødé ✓", "x" * DEBUG_STRING_CAP):
            enc = _encode_string("STRING", value)
            buf = bytearray(enc.ljust(127, b"\x00"))
            assert _decode_string("STRING", buf, 127) == value


class TestCap:
    def test_string_truncates_to_the_cap(self):
        enc = _encode_string("STRING", "y" * 200)
        assert enc[0] == DEBUG_STRING_CAP
        assert len(enc) == 1 + DEBUG_STRING_CAP

    @pytest.mark.parametrize("ch", ["é", "✓", "𝄞"])  # 2, 3 and 4 byte sequences
    def test_truncation_never_splits_a_character(self, ch):
        # A blind byte slice at 126 would leave half a character behind, which
        # comes back as a replacement char. The cap is a BYTE budget, so the
        # cut has to walk back off any continuation byte.
        enc = _encode_string("STRING", ch * 200)
        payload = enc[1:]
        assert len(payload) <= DEBUG_STRING_CAP
        assert enc[0] == len(payload)
        payload.decode("utf-8")               # strict: raises if split
        assert "�" not in payload.decode("utf-8")

    def test_wstring_truncates_to_the_cap_in_units(self):
        enc = _encode_string("WSTRING", ("z" * 200).encode("utf-16-le"))
        assert enc[0] == DEBUG_STRING_CAP
        assert len(enc) == 1 + DEBUG_STRING_CAP * 2

    def test_wstring_drops_a_trailing_odd_byte(self):
        # An odd length is malformed, and the encoder answers by dropping the
        # trailing byte rather than refusing the value. Named for what it does:
        # it used to be called "rejects_an_odd_byte_count" while asserting a
        # successful one-unit encode, so the name argued against the assertion.
        enc = _encode_string("WSTRING", b"abc")
        assert enc[0] == 1 and len(enc) == 3

    def test_wstring_truncation_does_not_split_a_surrogate_pair(self):
        # Cutting at the cap must not leave a lone high surrogate: that is not a
        # shorter string, it is one that raises on decode. 125 plain characters
        # plus an emoji is 127 code units, so the naive cut at 126 lands exactly
        # between the halves of the pair.
        value = ("a" * 125 + "\U0001F600").encode("utf-16-le")
        enc = _encode_string("WSTRING", value)
        units = enc[0]
        payload = enc[1 : 1 + units * 2]
        assert units == 125                      # the pair dropped whole
        payload.decode("utf-16-le")              # raises if a surrogate is split


class TestThroughTheDebugSurface:
    def test_read_decodes_what_the_runtime_returned(self):
        args = FakeArgs(bytes([5]) + b"hello")
        assert debug_read_value(args, 0, 21, "STRING") == "hello"

    def test_write_hands_the_runtime_the_wire_form(self):
        args = FakeArgs()
        assert debug_write_value(args, 0, 21, "STRING", "hello") is True
        assert args.written == bytes([5]) + b"hello"

    def test_write_reports_a_refusal(self):
        args = FakeArgs(status=0x81)  # STATUS_OUT_OF_BOUNDS
        assert debug_write_value(args, 0, 21, "STRING", "hello") is False

    def test_wstring_write_hands_over_utf16le(self):
        args = FakeArgs(width=253)
        assert debug_write_value(args, 0, 22, "WSTRING", "hi") is True
        assert args.written == bytes([2]) + "hi".encode("utf-16-le")


class TestTypeMapping:
    def test_string_maps_to_ua_string(self):
        assert map_plc_to_opcua_type("STRING") == ua.VariantType.String

    def test_wstring_maps_to_bytestring_not_the_variant_fallback(self):
        # Falling through to the VariantType.Variant default made asyncua
        # serialise a nested Variant around an int and die encoding the
        # RESPONSE, which the client saw as BadInternalError.
        assert map_plc_to_opcua_type("WSTRING") == ua.VariantType.ByteString
        assert map_plc_to_opcua_type("WSTRING") != ua.VariantType.Variant
