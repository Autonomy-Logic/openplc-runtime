"""Behavioural tests for installing the I/O image sizes from an upload.

The sizes belong to the project and are derived rather than chosen, so the
interesting behaviour is not "can a file be copied" — it is the present/absent
contract that keeps the project authoritative, and one case where this file
deliberately differs from retain.conf:

  * an upload carrying image.conf sizes the device;
  * an upload WITHOUT one removes the device's copy, so the runtime sizes the
    image from the program it just loaded. Not because absence is an
    instruction — it is not, unlike retain's — but because a STALE file keeps
    the previous project's memory reserved for a program that is gone;
  * a stanza the core could not honour is refused loudly, and does not leave the
    previous project's sizes quietly in force.

The last two are the cases that would otherwise be invisible: nothing on a
running device shows you that its image is sized for the program before last.
"""

import os

import pytest

from webserver import image_config, plcapp_management


@pytest.fixture(autouse=True)
def isolated_conf(tmp_path, monkeypatch):
    """Point the install destination at a temp file, never a real runtime root."""
    dest = tmp_path / "runtime" / "image.conf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(plcapp_management, "IMAGE_CONF_PATH", dest)
    return dest


@pytest.fixture()
def upload(tmp_path):
    """An extracted upload directory, as safe_extract would leave it."""
    d = tmp_path / "generated"
    d.mkdir()
    return d


def write_conf(directory, version=image_config.IMAGE_CONF_FORMAT_VERSION, **sizes):
    """An upload's image.conf, in the format the current editor emits.

    Every value carries the unit of the ADDRESS its table stores, so the number
    and its unit cannot disagree. `version=None` omits the declaration, which a
    reader must refuse rather than guess at.
    """
    lines = [] if version is None else [f"format_version={version}"]
    lines += [f"{k}={v} {image_config.IMAGE_TABLE_UNITS[k]}" for k, v in sizes.items()]
    (directory / "image.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_raw_conf(directory, body):
    """An image.conf written byte for byte, for the malformed cases."""
    (directory / "image.conf").write_text(body, encoding="utf-8")


def installed(dest):
    """Just the counts of the installed file."""
    _version, sizes, _units = image_config.read_image_conf_file(dest)
    return sizes


class TestPresentAbsentContract:
    def test_an_upload_carrying_sizes_installs_them(self, upload, isolated_conf):
        write_conf(upload, int_output=4096, bool_output=64, int_memory=20)
        plcapp_management.apply_image_conf(str(upload))

        assert isolated_conf.exists()
        sizes = installed(isolated_conf)
        assert sizes["int_output"] == 4096
        assert sizes["bool_output"] == 64
        assert sizes["int_memory"] == 20

    def test_every_table_is_written_including_the_zeros(self, upload, isolated_conf):
        # "Absent means zero" is an editor-side convention; the C parser should
        # not have to know it.
        write_conf(upload, int_output=8)
        plcapp_management.apply_image_conf(str(upload))

        body = isolated_conf.read_text(encoding="utf-8")
        for key in image_config.IMAGE_TABLE_KEYS:
            assert f"{key}=" in body
        assert "bool_input=0" in body

    def test_an_upload_without_the_file_removes_a_stale_copy(self, upload, isolated_conf):
        # THE CASE THAT MATTERS. A previous project sized the device for 4096
        # output words; this upload says nothing. Leaving the old file in place
        # would keep 4096 reserved for a program that is no longer here.
        write_conf(upload, int_output=4096)
        plcapp_management.apply_image_conf(str(upload))
        assert isolated_conf.exists()

        os.remove(upload / "image.conf")
        plcapp_management.apply_image_conf(str(upload))

        assert not isolated_conf.exists()

    def test_an_upload_without_the_file_and_no_copy_is_a_no_op(self, upload, isolated_conf):
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()


class TestRefusal:
    def test_a_table_beyond_the_abi_limit_is_refused(self, upload, isolated_conf):
        write_conf(upload, int_output=image_config.MAX_TABLE_ELEMENTS + 1)
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()

    def test_the_abi_limit_itself_is_accepted(self, upload, isolated_conf):
        # It is a fact of the ABI, not a policy ceiling, so the boundary value
        # is legal and only what exceeds it is not.
        write_conf(upload, int_output=image_config.MAX_TABLE_ELEMENTS)
        plcapp_management.apply_image_conf(str(upload))
        assert installed(isolated_conf)["int_output"] == image_config.MAX_TABLE_ELEMENTS

    def test_a_negative_size_is_refused(self, upload, isolated_conf):
        write_conf(upload, int_memory=-1)
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()

    def test_a_non_utf8_file_is_refused_rather_than_raising(self, upload, isolated_conf):
        # The file comes from an upload, so its bytes are whatever was sent.
        # UnicodeDecodeError used to escape to app.py, which answers
        # "Unexpected error: ..." to the client.
        (upload / "image.conf").write_bytes(b"int_output=\xff\xfe\n")
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()

    def test_a_directory_named_image_conf_is_refused_rather_than_raising(
        self, upload, isolated_conf
    ):
        (upload / "image.conf").mkdir()
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()

    def test_a_garbled_value_is_refused_rather_than_raising(self, upload, isolated_conf):
        (upload / "image.conf").write_text("int_output=lots\n", encoding="utf-8")
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()

    def test_a_refusal_does_not_leave_the_previous_sizes_in_force(self, upload, isolated_conf):
        write_conf(upload, int_output=4096)
        plcapp_management.apply_image_conf(str(upload))
        assert isolated_conf.exists()

        write_conf(upload, int_output=image_config.MAX_TABLE_ELEMENTS + 1)
        plcapp_management.apply_image_conf(str(upload))

        # Not "still 4096": the device must not stay sized by a project the user
        # is no longer running.
        assert not isolated_conf.exists()

    def test_all_fourteen_or_none(self, upload, isolated_conf):
        # The tables size interlocking storage that one allocation hands out
        # together, so a single bad value refuses the whole stanza rather than
        # installing the thirteen that were fine.
        write_conf(upload, int_output=8, bool_output=64, lint_memory=-3)
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()


class TestParser:
    def test_a_missing_file_reads_as_every_table_zero(self, tmp_path):
        _v, sizes, _u = image_config.read_image_conf_file(tmp_path / "nope.conf")
        assert sizes == {key: 0 for key in image_config.IMAGE_TABLE_KEYS}

    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        p = tmp_path / "image.conf"
        p.write_text("# a comment\n\nint_output=12\n   \n", encoding="utf-8")
        assert image_config.read_image_conf_file(p)[1]["int_output"] == 12

    def test_an_unknown_table_is_ignored_rather_than_refused(self, tmp_path, upload, isolated_conf):
        # A newer editor emitting a table this runtime does not have must not
        # fail the upload; the core would ignore it anyway.
        write_raw_conf(upload, "format_version=2\nint_output=8 words\nbyte_memory=64 bytes\n")
        plcapp_management.apply_image_conf(str(upload))
        assert installed(isolated_conf)["int_output"] == 8
        assert "byte_memory" not in isolated_conf.read_text(encoding="utf-8")

    def test_the_installed_file_is_byte_stable_for_the_same_sizes(self, upload, isolated_conf):
        write_conf(upload, int_output=8, bool_output=64)
        plcapp_management.apply_image_conf(str(upload))
        first = isolated_conf.read_text(encoding="utf-8")

        # Same sizes, different order in the uploaded file.
        write_conf(upload, bool_output=64, int_output=8)
        plcapp_management.apply_image_conf(str(upload))

        assert isolated_conf.read_text(encoding="utf-8") == first

    def test_no_key_for_a_table_the_runtime_does_not_have(self):
        # byte_input and byte_output exist; byte_memory does not, so %MB has no
        # storage on this runtime at all.
        assert "byte_input" in image_config.IMAGE_TABLE_KEYS
        assert "byte_memory" not in image_config.IMAGE_TABLE_KEYS
        assert len(image_config.IMAGE_TABLE_KEYS) == 14


class TestFormatVersion:
    """The version is checked before anything else, and refuses the whole file.

    Every check below it reads the values by this version's rules, so a file
    from a format we do not know is not a file with fourteen suspicious numbers
    in it -- it is a file we cannot claim to have understood.
    """

    def test_a_file_with_no_version_is_refused(self, upload, isolated_conf):
        write_conf(upload, version=None, int_output=8)
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()

    def test_a_future_version_is_refused_rather_than_guessed_at(self, upload, isolated_conf):
        write_conf(upload, version=3, int_output=8)
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()

    def test_a_refused_version_does_not_leave_the_previous_sizes_in_force(
        self, upload, isolated_conf
    ):
        # The device would otherwise keep running sized by a project that is no
        # longer on it, which is the whole reason a stale file is worse than none.
        isolated_conf.write_text("format_version=2\nint_output=4096 words\n", encoding="utf-8")
        write_conf(upload, version=None, int_output=8)
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()

    def test_the_installed_file_declares_the_version(self, upload, isolated_conf):
        write_conf(upload, int_output=8)
        plcapp_management.apply_image_conf(str(upload))
        assert "format_version=2" in isolated_conf.read_text(encoding="utf-8")


class TestUnits:
    def test_nothing_here_converts_bits_to_bytes(self, upload, isolated_conf):
        # The BOOL tables travel in BITS, because %QX addresses bits, and the
        # core converts to the [N][8] shape its storage has. Converting here
        # too is how the two sides end up disagreeing by a factor of eight with
        # no diagnostic anywhere, so 64 bits in must be 64 bits out.
        write_conf(upload, bool_output=64)
        plcapp_management.apply_image_conf(str(upload))
        assert installed(isolated_conf)["bool_output"] == 64

    def test_every_table_is_written_with_its_own_unit(self, upload, isolated_conf):
        write_conf(upload, int_output=8)
        plcapp_management.apply_image_conf(str(upload))
        body = isolated_conf.read_text(encoding="utf-8")
        for key, unit in image_config.IMAGE_TABLE_UNITS.items():
            assert f"{key}=" in body
            assert body.split(f"{key}=")[1].split("\n")[0].endswith(unit)

    def test_a_value_in_the_wrong_unit_is_refused(self, upload, isolated_conf):
        # The case the unit word exists for. Bytes is a perfectly plausible
        # unit for bool_output -- its STORAGE is in bytes -- and reading 64 as
        # bytes rather than bits allocates eight times what was asked for.
        write_raw_conf(upload, "format_version=2\nbool_output=64 bytes\n")
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()

    def test_a_value_with_no_unit_is_refused(self, upload, isolated_conf):
        # Which is exactly what a version-1 file looks like.
        write_raw_conf(upload, "format_version=2\nint_output=8\n")
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()


class TestRefusalMessages:
    """The message is the point of validating here rather than in the core.

    This module's own docstring says so: refusing it here, with a line in the
    build log the user is already watching, is the only place a person sees it.
    A message that names the wrong mistake sends them looking for something
    that is not there.
    """

    def test_a_non_integer_count_says_so(self):
        # Used to read "cannot be negative (got -1)", because the parse failure
        # was stored as -1 and fell into the negative branch.
        with pytest.raises(image_config.ImageConfigError) as excinfo:
            image_config.validate_table_count("int_output", None, "words")
        assert "whole number" in str(excinfo.value)
        assert "negative" not in str(excinfo.value)

    def test_an_actually_negative_count_still_says_negative(self):
        with pytest.raises(image_config.ImageConfigError) as excinfo:
            image_config.validate_table_count("int_output", -3, "words")
        assert "negative" in str(excinfo.value)
        assert "-3" in str(excinfo.value)

    def test_a_garbled_value_in_a_real_file_reaches_the_right_message(self, upload, isolated_conf):
        # End to end, because the sentinel is what connects the two.
        write_raw_conf(upload, "format_version=2\nint_output=4.5 words\n")
        _v, sizes, units = image_config.read_image_conf_file(upload / "image.conf")
        with pytest.raises(image_config.ImageConfigError) as excinfo:
            image_config.validate_image_conf(2, sizes, units)
        assert "whole number" in str(excinfo.value)


class TestCeiling:
    """The ABI ceiling is in ELEMENTS, and the file is not.

    Comparing a bit count against an element ceiling would refuse every legal
    image above 8192 bytes -- eight times early, and by exactly the unit
    confusion the format was changed to remove.
    """

    def test_a_word_table_is_capped_at_the_abi_limit(self, upload, isolated_conf):
        write_conf(upload, int_output=image_config.MAX_TABLE_ELEMENTS)
        plcapp_management.apply_image_conf(str(upload))
        assert isolated_conf.exists()

    def test_a_word_table_one_past_the_limit_is_refused(self, upload, isolated_conf):
        write_conf(upload, int_output=image_config.MAX_TABLE_ELEMENTS + 1)
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()

    def test_a_bit_table_may_reach_eight_bits_per_element(self, upload, isolated_conf):
        # 65536 elements of bool_output is 524288 bits, and every one of them
        # is addressable. This is the case a ceiling in the wrong unit breaks.
        write_conf(upload, bool_output=image_config.MAX_TABLE_ELEMENTS * 8)
        plcapp_management.apply_image_conf(str(upload))
        assert isolated_conf.exists()
        assert installed(isolated_conf)["bool_output"] == image_config.MAX_TABLE_ELEMENTS * 8

    def test_a_bit_table_one_bit_past_the_limit_is_refused(self, upload, isolated_conf):
        write_conf(upload, bool_output=image_config.MAX_TABLE_ELEMENTS * 8 + 1)
        plcapp_management.apply_image_conf(str(upload))
        assert not isolated_conf.exists()

    def test_the_ceiling_is_reported_in_the_unit_that_was_asked_for(self):
        with pytest.raises(image_config.ImageConfigError) as excinfo:
            image_config.validate_table_count(
                "bool_output", image_config.MAX_TABLE_ELEMENTS * 8 + 1, "bits"
            )
        # A message quoting an element ceiling against a bit count is the same
        # ambiguity in prose.
        assert "bits" in str(excinfo.value)


class TestLogging:
    def test_the_log_line_names_only_the_tables_actually_sized(self):
        sizes = {key: 0 for key in image_config.IMAGE_TABLE_KEYS}
        sizes["int_output"] = 4096
        line = image_config.describe_image_conf(sizes)
        # The unit travels with the number here too: a log line saying "4096"
        # for a bit table would be the same ambiguity the format removed.
        assert line == "int_output=4096 words"

    def test_an_empty_image_says_so_rather_than_listing_nothing(self):
        sizes = {key: 0 for key in image_config.IMAGE_TABLE_KEYS}
        assert image_config.describe_image_conf(sizes) == "every table zero"
