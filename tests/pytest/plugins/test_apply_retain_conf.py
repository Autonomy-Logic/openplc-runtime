"""Behavioural tests for installing persistent-storage settings from an upload.

Retain settings belong to the project, so the interesting behaviour is not "can
a file be copied" — it is the present/absent contract that makes the project
authoritative:

  * an upload carrying retain.conf configures the device;
  * an upload WITHOUT one un-configures it, which is how a target whose VPP owns
    retention switches the built-in file store off;
  * a stanza the core could not honour is refused loudly, and does not leave the
    previous project's settings quietly in force.

The last one is the case that would otherwise be invisible: a path whose parent
does not exist produces a store that accepts every write and commits none.
"""

import os

import pytest

from webserver import plcapp_management, retain_config


@pytest.fixture(autouse=True)
def isolated_conf(tmp_path, monkeypatch):
    """Point the install destination at a temp file, never a real runtime root."""
    dest = tmp_path / "runtime" / "retain.conf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(plcapp_management, "RETAIN_CONF_PATH", dest)
    return dest


@pytest.fixture()
def upload(tmp_path):
    """An extracted upload directory, as safe_extract would leave it."""
    d = tmp_path / "generated"
    d.mkdir()
    return d


def write_conf(directory, *, enabled=True, path=None, flush_seconds=5):
    store = path if path is not None else str(directory / "retain.bin")
    (directory / "retain.conf").write_text(
        f"enabled={'1' if enabled else '0'}\npath={store}\nflush_seconds={flush_seconds}\n",
        encoding="utf-8",
    )
    return store


def test_resolves_an_empty_path_to_this_devices_default(upload, isolated_conf):
    """The case hardware testing caught.

    The editor emits `path=` to mean "use this device's default" — it does not
    know the device's filesystem layout. The core, though, treats
    enabled-with-no-path as a misconfiguration and leaves the store OFF, so
    shipping the empty value straight through turned "use the default" into
    "silently no retention". Installing the PARSED stanza is what fixes it: the
    reader substitutes the default, and the file the core reads names a real
    location.
    """
    write_conf(upload, enabled=True, path="")

    plcapp_management.apply_retain_conf(str(upload))

    assert isolated_conf.exists()
    installed = isolated_conf.read_text(encoding="utf-8")
    assert "path=\n" not in installed, "an empty path would leave the store off"
    assert f"path={retain_config.DEFAULT_RETAIN_PATH}\n" in installed


def test_a_path_the_project_did_name_is_left_alone(upload, isolated_conf):
    store = write_conf(upload, enabled=True)

    plcapp_management.apply_retain_conf(str(upload))

    assert retain_config.read_retain_conf_file(isolated_conf)["path"] == store


def test_installs_settings_the_upload_carries(upload, isolated_conf):
    store = write_conf(upload, enabled=True, flush_seconds=7)

    plcapp_management.apply_retain_conf(str(upload))

    assert isolated_conf.exists()
    cfg = retain_config.read_retain_conf_file(isolated_conf)
    assert cfg == {"enabled": True, "path": store, "flushSeconds": 7}


def test_an_upload_without_settings_removes_the_devices_copy(upload, isolated_conf):
    """The absent case is load-bearing, not tidiness.

    It is how a VPP that declares `hidesNativeScreens: ['persistent-storage']`
    turns the built-in store off: the editor emits no retain.conf, and the
    built-in store then finds no config and declines the role.
    """
    isolated_conf.write_text("enabled=1\npath=/tmp/old.bin\nflush_seconds=5\n", encoding="utf-8")

    plcapp_management.apply_retain_conf(str(upload))  # no retain.conf in the upload

    assert not isolated_conf.exists()


def test_absent_settings_on_an_unconfigured_device_is_not_an_error(upload, isolated_conf):
    plcapp_management.apply_retain_conf(str(upload))
    assert not isolated_conf.exists()


def test_refuses_a_path_whose_directory_does_not_exist(upload, isolated_conf):
    """Refused at install, not discovered as a store that never writes."""
    write_conf(upload, enabled=True, path="/nonexistent-directory-xyz/retain.bin")

    plcapp_management.apply_retain_conf(str(upload))

    assert not isolated_conf.exists()


def test_a_refused_stanza_does_not_leave_the_previous_project_in_force(upload, isolated_conf):
    """Half-applied is worse than unconfigured.

    The user would be looking at a device configured by a project they are no
    longer running.
    """
    isolated_conf.write_text("enabled=1\npath=/tmp/old.bin\nflush_seconds=5\n", encoding="utf-8")
    write_conf(upload, enabled=True, path="relative/not/absolute.bin")

    plcapp_management.apply_retain_conf(str(upload))

    assert not isolated_conf.exists()


def test_refuses_a_flush_period_outside_the_bounds_the_core_accepts(upload, isolated_conf):
    write_conf(upload, enabled=True, flush_seconds=retain_config.MAX_FLUSH_SECONDS + 1)

    plcapp_management.apply_retain_conf(str(upload))

    assert not isolated_conf.exists()


def test_a_disabled_stanza_is_installed_without_validating_its_flush_period(upload, isolated_conf):
    """`flushSeconds` is as dead as `path` while storage is off.

    Refusing over it would delete the device's existing config for a value
    nothing reads. The case that makes this concrete: a later release tightens
    MAX_FLUSH_SECONDS, and every project still carrying the old value has its
    whole retain.conf refused on the next upload — including projects that had
    storage switched off anyway.
    """
    write_conf(upload, enabled=False, flush_seconds=retain_config.MAX_FLUSH_SECONDS + 1)

    plcapp_management.apply_retain_conf(str(upload))

    assert isolated_conf.exists()
    assert retain_config.read_retain_conf_file(isolated_conf)["enabled"] is False


def test_a_disabled_stanza_is_installed_without_validating_its_path(upload, isolated_conf):
    """Nothing will be written there, so an unusable path is not worth refusing.

    Keeping the stanza matters: it is what the project says, and round-tripping
    it means the screen shows the same thing the device holds.
    """
    write_conf(upload, enabled=False, path="/nonexistent-directory-xyz/retain.bin")

    plcapp_management.apply_retain_conf(str(upload))

    assert isolated_conf.exists()
    assert retain_config.read_retain_conf_file(isolated_conf)["enabled"] is False


def test_installing_replaces_the_previous_projects_settings(upload, isolated_conf):
    isolated_conf.write_text("enabled=1\npath=/tmp/old.bin\nflush_seconds=99\n", encoding="utf-8")
    store = write_conf(upload, enabled=True, flush_seconds=11)

    plcapp_management.apply_retain_conf(str(upload))

    cfg = retain_config.read_retain_conf_file(isolated_conf)
    assert cfg["path"] == store
    assert cfg["flushSeconds"] == 11
