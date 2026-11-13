"""The status row: a dot coloured by tone, and a few words beside it.

status_for decides both from the engine's snapshot, with no Tk; the panel only paints
them. The dot is the whole "connected" message, the text is the plan when one is known,
and otherwise it stays empty until something is wrong.
"""

import pytest

import ui_window
from claude_check import ClaudeStatus
from config import Config
from models import DeepSeekSnapshot, Engine, OpenAISnapshot, UsageSnapshot
from ui_window import status_for


def claude(**fields):
    """A snapshot whose usage call worked."""
    return UsageSnapshot(cli_available=True, **fields)


def logged_in(plan=None):
    return ClaudeStatus(installed=True, authenticated=True, plan=plan)


NOT_LOGGED_IN = ClaudeStatus(installed=True, authenticated=False)
NOT_INSTALLED = ClaudeStatus(installed=False)


class TestClaudeWithUsageData:
    """A snapshot that has data means connected, whatever the CLI or the last error says."""

    @pytest.mark.parametrize("plan, label", [("max", "Max"), ("pro", "Pro"), ("team", "Team"),
                                             ("some_new_plan", "Some New Plan")])
    def test_the_plan_the_cli_reports_is_the_text(self, plan, label):
        assert status_for(Engine.CLAUDE, claude(), logged_in(plan)) == ("ok", label)

    def test_no_plan_known_is_just_the_dot(self):
        assert status_for(Engine.CLAUDE, claude(), logged_in()) == ("ok", "")
        assert status_for(Engine.CLAUDE, claude(), None) == ("ok", "")

    def test_extra_usage_means_a_max_plan_when_the_cli_names_none(self):
        extra = claude(extra_enabled=True)
        assert status_for(Engine.CLAUDE, extra, logged_in()) == ("ok", "Max")
        assert status_for(Engine.CLAUDE, extra, None) == ("ok", "Max")

    def test_the_plan_the_cli_reports_beats_the_extra_usage_guess(self):
        assert status_for(Engine.CLAUDE, claude(extra_enabled=True), logged_in("pro")) == ("ok", "Pro")

    def test_it_beats_an_error_and_a_logged_out_cli(self):
        assert status_for(Engine.CLAUDE, claude(), NOT_LOGGED_IN, "token expired") == ("ok", "")


class TestClaudeWithoutUsageData:
    def test_a_logged_in_cli_is_connected_even_with_the_api_blocked(self):
        assert status_for(Engine.CLAUDE, None, logged_in("max"), "API restricted (429)") == ("ok", "Max")

    def test_a_logged_in_cli_with_no_plan_is_just_the_dot(self):
        assert status_for(Engine.CLAUDE, None, logged_in()) == ("ok", "")

    @pytest.mark.parametrize("error", ["OAuth: refresh failed", "Token expired", "TOKEN EXPIRED (401)"])
    def test_an_expired_session_is_a_warning(self, error):
        assert status_for(Engine.CLAUDE, None, NOT_LOGGED_IN, error) == ("warn", "Session expired")

    @pytest.mark.parametrize("error", ["credentials not found", "No OAuth token", "no oauth credentials"])
    def test_no_login_is_a_warning(self, error):
        assert status_for(Engine.CLAUDE, None, NOT_LOGGED_IN, error) == ("warn", "Not logged in")

    @pytest.mark.parametrize("error", ["API access restricted (429)", "HTTP 429", "Restricted", "Rate Limit hit"])
    def test_a_refused_request_wants_attention_not_a_warning(self, error):
        assert status_for(Engine.CLAUDE, None, NOT_LOGGED_IN, error) == ("attention", "API restricted")

    def test_any_other_error_is_a_connection_error(self):
        assert status_for(Engine.CLAUDE, None, NOT_LOGGED_IN, "boom") == ("warn", "Connection error")

    def test_the_first_matching_error_wins(self):
        assert status_for(Engine.CLAUDE, None, None, "token expired after a 429") == ("warn", "Session expired")
        assert status_for(Engine.CLAUDE, None, None, "restricted: not found") == ("warn", "Not logged in")

    def test_an_error_is_read_before_the_cli_is_asked_about_login(self):
        assert status_for(Engine.CLAUDE, None, NOT_INSTALLED, "boom") == ("warn", "Connection error")

    def test_an_empty_error_is_no_error(self):
        assert status_for(Engine.CLAUDE, None, NOT_LOGGED_IN, "") == ("attention", "Not logged in")

    def test_a_cli_that_is_not_logged_in_wants_attention(self):
        assert status_for(Engine.CLAUDE, None, NOT_LOGGED_IN) == ("attention", "Not logged in")

    def test_a_missing_cli_is_a_warning(self):
        assert status_for(Engine.CLAUDE, None, NOT_INSTALLED) == ("warn", "Claude CLI not found")

    def test_nothing_known_yet_is_a_quiet_dot(self):
        assert status_for(Engine.CLAUDE, None, None) == ("muted", "")


def codex(**fields):
    return OpenAISnapshot(**fields)


class TestCodex:
    def test_nothing_known_yet_is_a_quiet_dot(self):
        assert status_for(Engine.CODEX, None) == ("muted", "")

    @pytest.mark.parametrize("plan, label", [("plus", "Plus"), ("prolite", "Pro Lite"), ("free", "Free"),
                                             ("some_new_plan", "Some New Plan")])
    def test_a_connected_account_names_its_plan(self, plan, label):
        assert status_for(Engine.CODEX, codex(available=True, plan_type=plan)) == ("ok", label)

    def test_no_plan_shows_the_credits_left(self):
        assert status_for(Engine.CODEX, codex(available=True, credits_remaining=3.5)) == ("ok", "$3.50 credits")

    def test_no_credits_left_is_still_a_figure(self):
        assert status_for(Engine.CODEX, codex(available=True, credits_remaining=0.0)) == ("ok", "$0.00 credits")

    def test_the_plan_beats_the_credits(self):
        snap = codex(available=True, plan_type="pro", credits_remaining=3.5)
        assert status_for(Engine.CODEX, snap) == ("ok", "Pro")

    def test_connected_with_nothing_to_say_is_just_the_dot(self):
        assert status_for(Engine.CODEX, codex(available=True)) == ("ok", "")

    def test_a_missing_login_is_a_warning(self):
        snap = codex(available=False, error_message="Not logged in. Run 'codex login' first")
        assert status_for(Engine.CODEX, snap) == ("warn", "Not logged in")

    def test_an_expired_token_is_a_warning(self):
        snap = codex(available=False, error_message="Access Token Expired")
        assert status_for(Engine.CODEX, snap) == ("warn", "Token expired")

    def test_the_login_hint_is_read_before_the_expiry(self):
        snap = codex(available=False, error_message="token expired - Run 'codex login'")
        assert status_for(Engine.CODEX, snap) == ("warn", "Not logged in")

    def test_any_other_error_is_a_connection_error(self):
        assert status_for(Engine.CODEX, codex(available=False, error_message="HTTP 500")) == (
            "warn", "Connection error")

    def test_unavailable_without_an_error_is_not_configured(self):
        assert status_for(Engine.CODEX, codex(available=False)) == ("attention", "Not configured")

    def test_a_plan_does_not_make_an_unavailable_account_connected(self):
        assert status_for(Engine.CODEX, codex(available=False, plan_type="pro")) == (
            "attention", "Not configured")


class TestDeepSeek:
    def test_nothing_known_yet_is_a_quiet_dot(self):
        assert status_for(Engine.DEEPSEEK, None) == ("muted", "")

    def test_balance_and_usage_is_just_the_dot(self):
        snap = DeepSeekSnapshot(balance_available=True, usage_available=True)
        assert status_for(Engine.DEEPSEEK, snap) == ("ok", "")

    def test_a_balance_alone_wants_attention(self):
        snap = DeepSeekSnapshot(balance_available=True, usage_available=False)
        assert status_for(Engine.DEEPSEEK, snap) == ("attention", "Balance only")

    def test_nobody_signed_in_is_quiet_but_says_so(self):
        snap = DeepSeekSnapshot(error_message="Not signed in", usage_error="Not signed in")
        assert status_for(Engine.DEEPSEEK, snap) == ("muted", "Not signed in")

    def test_an_expired_session_is_a_warning(self):
        snap = DeepSeekSnapshot(error_message="Platform said no", usage_error="Session expired")
        assert status_for(Engine.DEEPSEEK, snap) == ("warn", "Session expired")

    def test_not_signed_in_is_read_before_the_expired_session(self):
        snap = DeepSeekSnapshot(error_message="Not signed in", usage_error="Session expired")
        assert status_for(Engine.DEEPSEEK, snap) == ("muted", "Not signed in")

    def test_any_other_reason_for_no_balance_is_worded_by_the_snapshot(self):
        snap = DeepSeekSnapshot(error_message="HTTP 500")
        assert status_for(Engine.DEEPSEEK, snap) == ("warn", "HTTP 500")

    def test_no_balance_and_no_reason_says_unavailable(self):
        assert status_for(Engine.DEEPSEEK, DeepSeekSnapshot()) == ("warn", "Balance unavailable")

    def test_a_balance_means_connected_even_with_an_old_error_left_over(self):
        snap = DeepSeekSnapshot(balance_available=True, usage_available=True, error_message="Not signed in")
        assert status_for(Engine.DEEPSEEK, snap) == ("ok", "")


class TestEachEngineReadsOnlyItsOwnThings:
    def test_codex_and_deepseek_ignore_the_claude_status_and_error(self):
        assert status_for(Engine.CODEX, codex(available=True, plan_type="pro"), NOT_INSTALLED, "boom") == (
            "ok", "Pro")
        snap = DeepSeekSnapshot(balance_available=True, usage_available=True)
        assert status_for(Engine.DEEPSEEK, snap, NOT_INSTALLED, "boom") == ("ok", "")


TONES = {"ok", "warn", "attention", "muted"}


class TestThePanelPaintsTheTone:
    def test_every_tone_has_a_colour_on_the_panel(self):
        panel = ui_window.ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None,
                                          on_exit=lambda: None, config=Config())
        assert set(ui_window._STATUS_TONES) == TONES
        colours = {tone: getattr(panel, attribute) for tone, attribute in ui_window._STATUS_TONES.items()}
        assert colours == {"ok": "#22c55e", "warn": "#ef4444", "attention": "#F59E0B", "muted": "#7f8899"}


@pytest.fixture
def panel(tk_root, monkeypatch):
    monkeypatch.setattr(ui_window, "get_exchange_rates", lambda: None)
    monkeypatch.setattr(ui_window, "check_claude_status", lambda: None)
    monkeypatch.setattr(ui_window, "save_config", lambda cfg: None)
    window = ui_window.ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None,
                                       on_exit=lambda: None, config=Config(),
                                       on_engine_change=lambda e: None)
    window._create_window()
    yield window
    window.destroy()


def shown(window):
    return (window._status_indicator.itemcget(window._status_dot, "fill"),
            window._status_text.cget("text"))


class TestTheRowOnTheRealPanel:
    def test_claude_with_data_is_a_green_dot_and_the_plan(self, panel):
        panel._active_engine = Engine.CLAUDE
        panel._claude_status = logged_in("max")
        panel._snapshot = claude()
        panel._update_status_display()
        assert shown(panel) == (panel.ok_color, "Max")

    def test_a_snapshot_with_data_clears_the_last_error(self, panel):
        panel._active_engine = Engine.CLAUDE
        panel._oauth_error = "API restricted (429)"
        panel._snapshot = claude()
        panel._update_status_display()
        assert panel._oauth_error is None

    def test_a_snapshot_still_waiting_in_the_queue_counts_too(self, panel):
        panel._active_engine = Engine.CLAUDE
        panel._oauth_error = "boom"
        panel._pending_snapshot = claude()
        panel._update_status_display()
        assert shown(panel) == (panel.ok_color, "")
        assert panel._oauth_error is None

    def test_a_snapshot_without_data_is_not_a_connection(self, panel):
        panel._active_engine = Engine.CLAUDE
        panel._oauth_error = "API restricted (429)"
        panel._snapshot = UsageSnapshot(cli_available=False)
        panel._update_status_display()
        assert shown(panel) == (panel.accent_color, "API restricted")
        assert panel._oauth_error == "API restricted (429)"

    def test_codex_and_deepseek_are_painted_from_their_own_snapshots(self, panel):
        panel._active_engine = Engine.CODEX
        panel._openai_snapshot = codex(available=False, error_message="HTTP 500")
        panel._update_status_display()
        assert shown(panel) == (panel.warn_color, "Connection error")
        panel._active_engine = Engine.DEEPSEEK
        panel._deepseek_snapshot = DeepSeekSnapshot(error_message="Not signed in")
        panel._update_status_display()
        assert shown(panel) == (panel.text_muted, "Not signed in")
