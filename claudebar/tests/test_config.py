"""A config file that cannot be used must not stop the app from starting, and a write
must reach the disk before it replaces the old file."""

import dataclasses
import json
import logging
import os
import time

import pytest

import config


def write_raw(data: bytes):
    path = config.get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def warnings(caplog):
    return [r for r in caplog.records if r.name == "claudebar" and r.levelno == logging.WARNING]


@pytest.mark.parametrize("raw", [
    b"{not json",                    # cut off in the middle of an edit
    b"\xff\xfe{\x00}\x00",           # not UTF-8 (an editor saved it as UTF-16)
    b"[]",                           # valid JSON that is not an object
    b'"a string"',
    b"null",
])
def test_an_unusable_file_loads_as_the_defaults_and_is_left_alone(raw, caplog):
    path = write_raw(raw)
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = config.load_config()
    assert loaded == config.Config()
    assert path.read_bytes() == raw, "the user's file must not be overwritten at load"
    assert len(warnings(caplog)) == 1


def test_an_unreadable_file_loads_as_the_defaults(caplog):
    """A sharing violation or a permissions error is an OSError; a directory in its place raises
    the same one on open."""
    config.get_config_path().mkdir(parents=True)
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        assert config.load_config() == config.Config()
    assert len(warnings(caplog)) == 1


def test_start_up_survives_an_unusable_file(caplog):
    """ensure_config_exists is what main calls; it must neither raise nor replace the file."""
    path = write_raw(b"[1, 2]")
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        assert config.ensure_config_exists() == config.Config()
    assert path.read_bytes() == b"[1, 2]"


def test_a_good_file_loads_without_a_warning(caplog):
    write_raw(b'{"currency": "EUR"}')
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        assert config.load_config().currency == "EUR"
    assert warnings(caplog) == []


def test_the_data_reaches_the_disk_before_it_replaces_the_file(tmp_path, monkeypatch):
    """Replacing a file with one that was never flushed can leave an empty file under the
    real name after a power cut."""
    calls = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda src, dst: (calls.append("replace"), real_replace(src, dst))[1])

    target = tmp_path / "out.json"
    config.atomic_write_text(target, "hello")

    assert calls == ["fsync", "replace"]
    assert target.read_text(encoding="utf-8") == "hello"
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"], "no temp file left behind"


# ------------------------------------------------------------ a file that could not be read

class LockedFile:
    """`open` as config.py sees it while an antivirus scan holds config.json: the first
    `failures` opens of the file raise the sharing violation, the rest read it."""

    def __init__(self, monkeypatch, failures):
        self.failures, self.opened = failures, 0
        self.pauses = []
        real_open = open

        def open_(path, *args, **kwargs):
            if str(path) == str(config.get_config_path()):
                self.opened += 1
                if self.opened <= self.failures:
                    raise PermissionError(13, "The process cannot access the file")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(config, "open", open_, raising=False)
        monkeypatch.setattr(time, "sleep", self.pauses.append)

    def release(self, monkeypatch):
        monkeypatch.delattr(config, "open")


def test_a_file_locked_for_a_moment_is_read_on_a_later_try(monkeypatch, caplog):
    write_raw(b'{"currency": "EUR", "refresh_interval": 90}')
    lock = LockedFile(monkeypatch, failures=2)
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = config.load_config()
    assert (loaded.currency, loaded.refresh_interval) == ("EUR", 90)
    assert warnings(caplog) == []
    assert lock.opened == 3 and sum(lock.pauses) < 0.5


def test_a_file_that_stays_locked_is_not_written_over(monkeypatch, caplog):
    """The app runs on the defaults, and the first save would write them, with the one field
    that changed, over the user's real settings."""
    original = b'{"currency": "RON", "warning_threshold": 70, "claude_enabled": false}'
    path = write_raw(original)
    lock = LockedFile(monkeypatch, failures=10 ** 6)
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = config.load_config()
    assert loaded == config.Config()
    assert len(warnings(caplog)) == 1, "one warning at load"
    assert lock.opened > 1, "it is tried again before it is given up"
    assert sum(lock.pauses) < 0.5, "the retries are short: this runs while the app starts"

    caplog.clear()
    loaded.window_x = 40                                  # a panel drag
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        config.save_config(loaded)
        config.save_config(loaded)                        # another one: tab switch, Settings
    assert path.read_bytes() == original
    assert len(warnings(caplog)) == 1, "one warning for the skipped writes, not one for each"
    assert [p.name for p in path.parent.iterdir()] == ["config.json"], "no temp file left behind"


def test_saving_works_again_once_a_load_succeeds(monkeypatch):
    path = write_raw(b'{"currency": "RON"}')
    lock = LockedFile(monkeypatch, failures=10 ** 6)
    config.load_config()
    config.save_config(config.Config(currency="EUR"))
    assert b"RON" in path.read_bytes()

    lock.release(monkeypatch)
    loaded = config.load_config()                         # the file is readable now
    assert loaded.currency == "RON"
    loaded.currency = "EUR"
    config.save_config(loaded)
    assert json.loads(path.read_text(encoding="utf-8"))["currency"] == "EUR"


def test_a_directory_in_the_files_place_is_not_written_over(caplog):
    """A real OSError on open, no stand-in: nothing here can save, and nothing is raised."""
    config.get_config_path().mkdir(parents=True)
    config.load_config()
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        config.save_config(config.Config())               # os.replace onto a directory would raise
    assert config.get_config_path().is_dir()


@pytest.mark.parametrize("raw", [b"{not json", b"[]", b"\xff\xfe{\x00}\x00"])
def test_an_unusable_file_is_still_written_whole_by_the_next_save(raw):
    """Unlike an unreadable one: the user may be mid-edit, but the content is known to be no
    settings at all, and the first save replaces it."""
    path = write_raw(raw)
    loaded = config.load_config()
    loaded.currency = "EUR"
    config.save_config(loaded)
    assert json.loads(path.read_text(encoding="utf-8"))["currency"] == "EUR"


def test_a_first_run_saves_its_defaults():
    config.ensure_config_exists()
    assert config.get_config_path().exists()


# ------------------------------------------------------------ the refresh interval's limits

def test_the_refresh_interval_is_kept_to_a_floor_and_a_ceiling_of_one_day():
    assert config.MIN_REFRESH_INTERVAL == 30
    assert config.MAX_REFRESH_INTERVAL == 24 * 60 * 60


@pytest.mark.parametrize("stored, expected", [
    (9999999, config.MAX_REFRESH_INTERVAL),      # Event.wait raises above threading.TIMEOUT_MAX
    (86401, config.MAX_REFRESH_INTERVAL),
    (86400, 86400),
    (300, 300),
    (30, 30),
    (29, config.MIN_REFRESH_INTERVAL),
    (0, config.MIN_REFRESH_INTERVAL),
    (-5, config.MIN_REFRESH_INTERVAL),
    ("7200", 7200),
    ("99999999", config.MAX_REFRESH_INTERVAL),
    (45.9, 45),
])
def test_a_stored_interval_is_clamped_at_load(stored, expected):
    write_raw(json.dumps({"refresh_interval": stored}).encode())
    assert config.load_config().refresh_interval == expected


@pytest.mark.parametrize("stored", ['"soon"', "null", "[1]", "Infinity", "NaN", "1e999"])
def test_an_interval_that_is_no_number_loads_as_the_default(stored):
    """Infinity and 1e999 are what json makes of a float no int can hold: int() raises OverflowError,
    which load_config let out of start-up."""
    write_raw(b'{"refresh_interval": ' + stored.encode() + b"}")
    assert config.load_config().refresh_interval == config.Config().refresh_interval


# ------------------------------------------------------------ every field, by its declared type

DEFAULTS = config.Config()


def load_with(**fields):
    write_raw(json.dumps(fields).encode())
    return config.load_config()


def reset_fields(caplog):
    """The field names the one warning about unusable values names, or [] when there is none."""
    found = [r.getMessage() for r in warnings(caplog) if "unusable values" in r.getMessage()]
    assert len(found) <= 1, "one warning for all the fields, not one for each"
    return [name for name in (f.name for f in dataclasses.fields(config.Config))
            if found and name in found[0]]


@pytest.mark.parametrize("field, bad", [
    ("warning_threshold", "high"),            # the string that made get_status_level raise TypeError
    ("warning_threshold", True),              # a JSON bool is not an integer
    ("warning_threshold", None),
    ("warning_threshold", 80.5),
    ("warning_threshold", -1),
    ("warning_threshold", 101),
    ("critical_threshold", "95"),
    ("critical_threshold", [95]),
    ("critical_threshold", 1000),
    ("claude_enabled", "false"),              # a string is not a boolean, and "false" is truthy
    ("codex_enabled", 0),
    ("deepseek_enabled", None),
    ("claude_enabled", "no"),
    ("currency", "XYZ"),
    ("currency", "usd"),
    ("currency", 5),
    ("currency", None),
    ("window_x", "left"),
    ("window_x", 12.5),
    ("window_x", True),
    ("window_y", [1]),
    ("window_y", "10"),
    ("panel_view", "both"),
    ("panel_view", 3),
    ("panel_scale", "big"),
    ("panel_scale", True),                    # a JSON bool is not a number
    ("panel_scale", 0.69),
    ("panel_scale", 2.51),
    ("panel_scale", 0),
    ("panel_scale", -1.5),
    ("panel_scale", [1.0]),
    ("panel_max_height", "tall"),
    ("panel_max_height", True),
    ("panel_max_height", 0),
    ("panel_max_height", -900),
    ("panel_max_height", 900.5),
    ("refresh_interval", "soon"),
    ("refresh_interval", True),
    ("refresh_interval", None),
    ("refresh_interval", [300]),
])
def test_a_field_of_the_wrong_type_or_range_is_its_default_and_the_others_keep_their_values(
        field, bad, caplog):
    mine = dict(warning_threshold=60, critical_threshold=90, claude_enabled=False, codex_enabled=False,
                deepseek_enabled=False, currency="RON", window_x=-1920, window_y=40,
                panel_view="forecast", refresh_interval=600)
    mine[field] = bad
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = load_with(**mine)

    assert getattr(loaded, field) == getattr(DEFAULTS, field)
    for name, value in mine.items():
        if name != field and name not in ("warning_threshold", "critical_threshold"):
            assert getattr(loaded, name) == value, f"{name} was reset along with {field}"
    if field not in ("warning_threshold", "critical_threshold"):
        assert (loaded.warning_threshold, loaded.critical_threshold) == (60, 90)
    assert field in reset_fields(caplog)


# ------------------------------------------------------------ the panel's scale and height

def test_the_panel_has_no_scale_or_height_of_its_own_until_asked():
    assert DEFAULTS.panel_scale is None and DEFAULTS.panel_max_height is None


@pytest.mark.parametrize("scale", [0.7, 1.0, 1.35, 2.5])
def test_a_panel_scale_in_range_survives_a_save_and_a_load(scale, caplog):
    config.save_config(config.Config(panel_scale=scale, panel_max_height=900))
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = config.load_config()
    assert (loaded.panel_scale, loaded.panel_max_height) == (scale, 900)
    assert warnings(caplog) == []


def test_a_whole_number_written_by_hand_is_a_good_scale(caplog):
    """json.dumps writes 2.0, but a person typing the file writes 2."""
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = load_with(panel_scale=2, panel_max_height=700)
    assert (loaded.panel_scale, loaded.panel_max_height) == (2, 700)
    assert warnings(caplog) == []


def test_null_is_the_automatic_choice_and_is_not_a_reason_to_warn(caplog):
    config.save_config(config.Config(panel_scale=1.2, panel_max_height=800))
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = load_with(panel_scale=None, panel_max_height=None)
    assert loaded.panel_scale is None and loaded.panel_max_height is None
    assert warnings(caplog) == []


@pytest.mark.parametrize("raw", [b'{"panel_scale": NaN}', b'{"panel_scale": Infinity}',
                                 b'{"panel_scale": -Infinity}'])
def test_a_scale_that_is_no_finite_number_is_the_default(raw, caplog):
    write_raw(raw)
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = config.load_config()
    assert loaded.panel_scale is None
    assert reset_fields(caplog) == ["panel_scale"]


def test_the_warning_names_the_fields_and_never_their_values(caplog):
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = load_with(warning_threshold="hunter2-high", currency="XYZ-secret", claude_enabled="false",
                           window_x="far-left", refresh_interval=600)
    assert reset_fields(caplog) == ["warning_threshold", "currency", "claude_enabled", "window_x"]
    text = " ".join(r.getMessage() for r in caplog.records)
    for value in ("hunter2-high", "XYZ-secret", "far-left", "false"):
        assert value not in text
    assert loaded.refresh_interval == 600


def test_the_values_a_save_writes_load_without_a_warning(caplog):
    saved = config.Config(refresh_interval=90, warning_threshold=0, critical_threshold=100, currency="RON",
                          claude_enabled=False, window_x=-1920, window_y=0, panel_view="forecast")
    config.save_config(saved)
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        assert config.load_config() == saved
    assert warnings(caplog) == []


def test_a_field_the_file_leaves_out_or_adds_is_not_a_reason_to_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = load_with(currency="EUR", some_future_setting=[1, 2])
    assert loaded.currency == "EUR" and loaded.warning_threshold == DEFAULTS.warning_threshold
    assert warnings(caplog) == []


@pytest.mark.parametrize("warning, critical", [(70, 70), (0, 0), (100, 100), (0, 100), (10, 11)])
def test_a_warning_at_or_below_the_critical_threshold_is_kept(warning, critical, caplog):
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = load_with(warning_threshold=warning, critical_threshold=critical)
    assert (loaded.warning_threshold, loaded.critical_threshold) == (warning, critical)
    assert warnings(caplog) == []


@pytest.mark.parametrize("fields", [
    dict(warning_threshold=90, critical_threshold=60),
    dict(warning_threshold=96, critical_threshold=95),
    dict(warning_threshold="x", critical_threshold=60),          # the default 80 would be above 60
    dict(warning_threshold=99),                                  # above the default critical, 95
])
def test_thresholds_in_the_wrong_order_both_fall_back(fields, caplog):
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        loaded = load_with(currency="EUR", **fields)
    assert (loaded.warning_threshold, loaded.critical_threshold) == (80, 95)
    assert reset_fields(caplog) == ["warning_threshold", "critical_threshold"]
    assert loaded.currency == "EUR"


def test_every_field_of_the_config_is_checked_by_its_declared_type():
    """A field added to Config later is checked without anyone listing it: its default, which a
    save writes, must pass the check that its declared type implies."""
    for f in dataclasses.fields(config.Config):
        assert config._usable(f, getattr(DEFAULTS, f.name)), f.name
        assert not config._usable(f, object()), f"{f.name} accepts anything"
