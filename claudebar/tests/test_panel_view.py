"""Which half of the panel is showing (Cost or Quota), and what the tabs say."""

import json

import config
import ui_window
from models import Engine


class TestPanelViewSetting:
    def test_it_defaults_to_cost(self):
        assert config.Config().panel_view == "cost"

    def test_a_choice_survives_a_restart(self):
        cfg = config.Config()
        cfg.panel_view = "forecast"
        config.save_config(cfg)
        assert config.load_config().panel_view == "forecast"

    def test_an_unknown_value_falls_back_to_cost(self):
        path = config.get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"panel_view": "bogus", "currency": "EUR"}), encoding="utf-8")
        loaded = config.load_config()
        assert loaded.panel_view == "cost"
        assert loaded.currency == "EUR"

    def test_a_config_from_before_the_setting_still_loads(self):
        path = config.get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"currency": "EUR"}), encoding="utf-8")
        assert config.load_config().panel_view == "cost"

    def test_a_non_string_value_falls_back_to_cost(self):
        path = config.get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"panel_view": 3}), encoding="utf-8")
        assert config.load_config().panel_view == "cost"


class TestTabTitles:
    def test_claude_and_codex_read_cost_and_quota(self):
        assert ui_window.tab_titles(Engine.CLAUDE) == ("Cost", "Quota")
        assert ui_window.tab_titles(Engine.CODEX) == ("Cost", "Quota")

    def test_deepseek_reads_spend_and_balance(self):
        assert ui_window.tab_titles(Engine.DEEPSEEK) == ("Spend", "Balance")

    def test_the_first_title_is_the_cost_view_and_the_second_the_forecast(self):
        assert ui_window.PANEL_VIEWS == ("cost", "forecast")
