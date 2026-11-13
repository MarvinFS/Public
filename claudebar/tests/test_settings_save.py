"""What the Settings dialog's Save does with the refresh interval typed into it.

The dialog is built without Tk: Save reads a few variables and writes the config.
"""

import json
import types

import pytest

import config
from ui_window import SettingsDialog


class Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


@pytest.fixture
def dialog():
    dialog = SettingsDialog.__new__(SettingsDialog)
    dialog.config = config.Config(refresh_interval=300)
    dialog.claude_var, dialog.codex_var, dialog.deepseek_var = Var(True), Var(True), Var(True)
    dialog.deepseek_token_var = Var("")
    dialog.currency_var = Var("USD")
    dialog.refresh_var = Var("300")
    dialog.error_label = types.SimpleNamespace(config=lambda **kw: None)
    dialog.dialog = types.SimpleNamespace(destroy=lambda: None)
    dialog._signin_thread, dialog._signin_cancel = None, False
    dialog.saved = []
    dialog.on_save = lambda: dialog.saved.append(dialog.config.refresh_interval)
    dialog.on_close = None
    dialog.parent = types.SimpleNamespace(attributes=lambda *a: None)
    return dialog


def stored():
    return json.loads(config.get_config_path().read_text(encoding="utf-8"))["refresh_interval"]


@pytest.mark.parametrize("typed, expected", [
    ("9999999", config.MAX_REFRESH_INTERVAL),    # Event.wait cannot wait that long
    ("86401", config.MAX_REFRESH_INTERVAL),
    ("86400", 86400),
    ("600", 600),
    ("5", config.MIN_REFRESH_INTERVAL),
    ("0", config.MIN_REFRESH_INTERVAL),
])
def test_the_interval_is_kept_within_its_limits_when_saved(dialog, typed, expected):
    dialog.refresh_var = Var(typed)
    dialog._save()
    assert dialog.config.refresh_interval == expected
    assert stored() == expected
    assert dialog.saved == [expected]


@pytest.mark.parametrize("typed", ["", "soon", "1e999", "12.5", "9" * 5000])
def test_an_entry_that_is_no_whole_number_keeps_the_interval_as_it_was(dialog, typed):
    dialog.refresh_var = Var(typed)
    dialog._save()
    assert dialog.config.refresh_interval == 300 and stored() == 300
