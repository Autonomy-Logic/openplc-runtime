"""How the Modbus slave fits its declared exposure to the image (RTOP-284).

These sit in ``tests/pytest`` rather than ``tests/pytest/modbus_slave`` on
purpose: the workflow ignores that directory, so a test placed there would
never run. They exercise pure functions and need only the pymodbus that the
workflow already installs for collection.

What they are protecting is a shrink that is invisible by construction. When
the exposure does not fit, the server answers Illegal Data Address for the
addresses it dropped -- correct, but indistinguishable over a network from a
server that was configured that way on purpose. The startup warning is the
only place the person who configured it can find out, and it has to fire for
every config shape, including the old-editor ones that are the reason the
shrink can happen at all.
"""

import importlib.util
import pathlib

import pytest

PLUGIN = (
    pathlib.Path(__file__).resolve().parents[2]
    / "core/src/drivers/plugins/python/modbus_slave/simple_modbus.py"
)


@pytest.fixture(scope="module")
def sm():
    spec = importlib.util.spec_from_file_location("simple_modbus_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def warnings(sm, monkeypatch):
    collected = []

    class CollectingLogger:
        def warn(self, message):
            collected.append(message)

        def info(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def debug(self, *args, **kwargs):
            pass

    monkeypatch.setattr(sm, "logger", CollectingLogger())
    return collected


def fit_and_report(sm, config, buffer_size):
    parsed = sm.parse_buffer_mapping_config(config, buffer_size)
    sm._log_clamped_segments(config, parsed)
    return parsed


def segmented(**counts):
    return {
        "buffer_mapping": {
            "holding_registers": {
                "qw_count": counts.get("qw", 0),
                "mw_count": counts.get("mw", 0),
                "md_count": counts.get("md", 0),
                "ml_count": counts.get("ml", 0),
            },
            "coils": {"qx_bits": counts.get("qx", 0), "mx_bits": counts.get("mx", 0)},
            "discrete_inputs": {"ix_bits": counts.get("ix", 0)},
            "input_registers": {"iw_count": counts.get("iw", 0)},
        }
    }


# --- the exposure is fitted to the image ---------------------------------


def test_segment_larger_than_the_image_is_cut_to_it(sm):
    parsed = sm.parse_buffer_mapping_config(segmented(qw=1024), 8)
    assert parsed["holding_registers"]["qw_count"] == 8


def test_bit_segments_get_eight_slots_per_image_element(sm):
    parsed = sm.parse_buffer_mapping_config(segmented(qx=8192), 8)
    assert parsed["coils"]["qx_bits"] == 64


def test_an_exposure_the_image_holds_is_left_alone(sm, warnings):
    parsed = fit_and_report(sm, segmented(qw=10, qx=16, ix=16, iw=10), 1024)
    assert parsed["holding_registers"]["qw_count"] == 10
    assert parsed["coils"]["qx_bits"] == 16
    assert warnings == []


# --- the warning covers every config shape, not just the segmented one ----


def test_the_legacy_shape_is_reported_when_it_shrinks(sm, warnings):
    # max_coils and friends: what an editor that predates image.conf uploads,
    # and the shape that used to shrink without a word.
    legacy = {"buffer_mapping": {"max_coils": 8192, "max_holding_registers": 1024}}
    parsed = fit_and_report(sm, legacy, 8)

    assert parsed["holding_registers"]["qw_count"] == 8
    assert parsed["coils"]["qx_bits"] == 64
    assert len(warnings) == 1
    assert "qw_count 1024 -> 8" in warnings[0]
    assert "qx_bits 8192 -> 64" in warnings[0]


def test_a_config_without_buffer_mapping_is_reported_when_it_shrinks(sm, warnings):
    # The defaults it falls back to are clamped exactly like a declared count,
    # so they are what was asked for.
    fit_and_report(sm, {"network_configuration": {"host": "0.0.0.0", "port": 502}}, 8)

    assert len(warnings) == 1
    assert "qw_count 1024 -> 8" in warnings[0]
    assert "ix_bits 8192 -> 64" in warnings[0]


def test_an_omitted_segment_of_a_segmented_config_is_reported(sm, warnings):
    partial = {"buffer_mapping": {"holding_registers": {"qw_count": 4}}}
    fit_and_report(sm, partial, 8)

    # qw_count fits; the segments the config never mentioned take the
    # generator's defaults and those are what shrink.
    assert len(warnings) == 1
    assert "qw_count" not in warnings[0]
    assert "mw_count 1024 -> 8" in warnings[0]


# --- the composed block fits one Modbus table -----------------------------


def test_the_register_block_never_exceeds_one_table(sm):
    ceiling = sm.MODBUS_MAX_ADDRESSES
    parsed = sm.parse_buffer_mapping_config(
        segmented(qw=ceiling, mw=ceiling, md=ceiling, ml=ceiling), ceiling
    )
    hr = parsed["holding_registers"]

    # The block lays the segments out as %QW | %MW | %MD | %ML, at two
    # registers per %MD and four per %ML.
    composed = hr["qw_count"] + hr["mw_count"] + hr["md_count"] * 2 + hr["ml_count"] * 4
    assert composed == ceiling


def test_the_coil_block_never_exceeds_one_table(sm):
    ceiling = sm.MODBUS_MAX_ADDRESSES
    parsed = sm.parse_buffer_mapping_config(
        segmented(qx=ceiling * sm.MAX_BITS, mx=ceiling * sm.MAX_BITS), ceiling
    )
    coils = parsed["coils"]

    assert coils["qx_bits"] + coils["mx_bits"] == ceiling


def test_trimming_takes_from_the_tail_so_earlier_segments_keep_their_addresses(sm):
    ceiling = sm.MODBUS_MAX_ADDRESSES
    parsed = sm.parse_buffer_mapping_config(
        segmented(qw=ceiling, mw=ceiling, md=ceiling, ml=ceiling), ceiling
    )
    hr = parsed["holding_registers"]

    # %QW starts at address 0 and is the segment a client is most likely to
    # already be reading, so it is the last one to lose anything.
    assert hr["qw_count"] == ceiling
    assert hr["ml_count"] == 0


def test_a_block_that_already_fits_is_not_trimmed(sm):
    parsed = sm.parse_buffer_mapping_config(segmented(qw=100, mw=100, md=100, ml=100), 1024)
    hr = parsed["holding_registers"]

    assert (hr["qw_count"], hr["mw_count"], hr["md_count"], hr["ml_count"]) == (100, 100, 100, 100)


def test_the_pdu_trim_is_reported_too(sm, warnings):
    ceiling = sm.MODBUS_MAX_ADDRESSES
    fit_and_report(sm, segmented(qw=ceiling, mw=ceiling, md=ceiling, ml=ceiling), ceiling)

    # The image held every segment; the address space did not. Same warning,
    # because from the configurer's side it is the same surprise.
    assert len(warnings) == 1
    assert "ml_count" in warnings[0]


# --- a malformed config must not take the server down ---------------------


@pytest.mark.parametrize("junk", ["muitos", None, -5, True, {"nested": 1}])
def test_a_count_that_is_not_a_count_falls_back_to_the_default(sm, junk):
    config = {"buffer_mapping": {"holding_registers": {"qw_count": junk}}}
    parsed = sm.parse_buffer_mapping_config(config, 4096)

    assert parsed["holding_registers"]["qw_count"] == sm.DEFAULT_HOLDING_REG_CONFIG["qw_count"]


def test_a_buffer_mapping_that_is_not_an_object_is_treated_as_absent(sm):
    parsed = sm.parse_buffer_mapping_config({"buffer_mapping": []}, 4096)

    assert parsed["format"] == "legacy"
    assert parsed["holding_registers"]["qw_count"] == 1024


def test_a_section_that_is_not_an_object_falls_back_to_defaults(sm):
    config = {"buffer_mapping": {"holding_registers": {"qw_count": 4}, "coils": "nope"}}
    parsed = sm.parse_buffer_mapping_config(config, 4096)

    assert parsed["holding_registers"]["qw_count"] == 4
    assert parsed["coils"]["qx_bits"] == sm.DEFAULT_COILS_CONFIG["qx_bits"]


# --- the shape the config declares is preserved ---------------------------


def test_the_segmented_shape_keeps_its_word_order(sm):
    config = segmented(qw=4)
    config["word_order"] = "low_word_first"
    assert sm.parse_buffer_mapping_config(config, 1024)["word_order"] == "low_word_first"


def test_the_legacy_shape_has_no_memory_segments(sm):
    legacy = {"buffer_mapping": {"max_holding_registers": 16, "max_coils": 16}}
    parsed = sm.parse_buffer_mapping_config(legacy, 1024)

    assert parsed["format"] == "legacy"
    assert parsed["holding_registers"]["mw_count"] == 0
    assert parsed["coils"]["mx_bits"] == 0
    assert parsed["word_order"] == "high_word_first"
