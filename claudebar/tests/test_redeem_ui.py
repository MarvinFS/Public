"""The "Use" action on the panel: when it is offered, that nothing is sent
before an explicit confirmation, that one confirmation sends exactly one
request, and what the panel says afterwards.

The panel is real (a withdrawn Tk window), but the confirmation dialog is a
stub and the redeem runner is a recorder: no request is ever made.
"""

import threading
import tkinter as tk
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import banked_resets as br
import reset_redeem as rr
import ui_window
from config import Config
from models import CombinedSnapshot, Engine, OpenAISnapshot, UsageSnapshot

UTC = timezone.utc
NOW = datetime.now(UTC)


def claude_banked(**kw):
    grant = {"id": "opus55-launch-promax-20260921", "label": "Opus launch reset",
             "resets_total": 1, "resets_left": 1, "ends_at": "2099-10-22T16:00:00+00:00",
             "clears": ["five_hour", "seven_day"], "paused": False, "usable_now": True,
             "use_requires_limit": False}
    grant.update(kw)
    return br.parse_claude_banked({"eligible": True, "next_grant_id": grant["id"], "grants": [grant]},
                                  NOW)


def codex_banked(applicable=1):
    return br.parse_codex_banked(
        {"available_count": 3, "applicable_available_count": applicable},
        {"credits": [{"id": "c-oct22", "status": "available", "reset_type": "codex_rate_limits",
                      "title": "Full reset (Monthly)", "expires_at": "2099-10-22T09:00:00Z"}]}, NOW)


def claude_snap(stale=False, banked="default", offset=0):
    """`offset` moves the collection time, in seconds, so tests never depend on
    the clock's tick."""
    return UsageSnapshot(
        timestamp=datetime.now() + timedelta(seconds=offset),
        session_percent=12.0, weekly_percent=39.0, weekly_resets_at=NOW.replace(year=2099),
        banked=claude_banked() if banked == "default" else banked, is_stale=stale,
        stale_since=datetime.now() if stale else None)


def codex_snap(applicable=1, stale=False):
    return OpenAISnapshot(
        session_available=False, weekly_percent=4.0, weekly_window_hours=720.0, available=True,
        weekly_resets_at=datetime(2099, 1, 1), banked=codex_banked(applicable), is_stale=stale,
        stale_since=datetime.now() if stale else None)


class StubDialog:
    """Stands in for the confirmation dialog and records what it was shown."""
    shown = []

    def __init__(self, parent, lines, on_confirm, on_cancel, style):
        self.lines, self.on_confirm, self.on_cancel = lines, on_confirm, on_cancel
        StubDialog.shown.append(self)


class Runner:
    """Stands in for run_attempt: records every attempt, optionally blocks."""

    def __init__(self, outcome=None):
        self.attempts = []
        self.gate = None
        self.outcome = outcome or rr.RedeemOutcome("success", "Reset applied · 2 limits refreshed", 2, True)

    def __call__(self, attempt):
        self.attempts.append(attempt)
        if self.gate is not None:
            self.gate.wait(5)
        return self.outcome


@pytest.fixture(scope="module")
def tk_root():
    """One hidden Tk root for the module: creating and destroying a root per test
    makes Tcl's init fail intermittently ("tcl_findLibrary")."""
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display for Tk: {e}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def panel(tk_root, monkeypatch):
    monkeypatch.setattr(ui_window, "get_exchange_rates", lambda: None)
    monkeypatch.setattr(ui_window, "check_claude_status", lambda: None)
    monkeypatch.setattr(ui_window, "save_config", lambda cfg: None)
    monkeypatch.setattr(ui_window, "ResetConfirmDialog", StubDialog)
    StubDialog.shown = []
    refreshes = []
    window = ui_window.ClaudeBarWindow(on_refresh=lambda: refreshes.append(1), on_settings=lambda: None,
                                       on_exit=lambda: None, config=Config(),
                                       on_engine_change=lambda e: None)
    window._create_window()
    window.refreshes = refreshes
    window.runner = Runner()
    window._run_attempt = window.runner
    yield window
    window.destroy()


def show(window, claude=None, openai=None, engine=Engine.CLAUDE):
    window._active_engine = engine
    window.update_combined(CombinedSnapshot(claude=claude, openai=openai))
    window._apply_snapshot_update()
    window._window.update()


def use_visible(window):
    return bool(window._banked_use.winfo_manager())


def finish(window):
    """Wait for the worker, then let the panel pick up its result."""
    window._redeem_thread.join(5)
    window._apply_redeem_result()
    window._window.update()


class TestOffered:
    def test_a_live_claimable_reset_shows_use(self, panel):
        show(panel, claude=claude_snap())
        assert use_visible(panel)
        assert panel._banked_use.cget("text") == "Use"
        assert panel._banked_hint.cget("text") == ""

    def test_cached_data_never_shows_use(self, panel):
        show(panel, claude=claude_snap(stale=True))
        assert not use_visible(panel)
        assert "cached" in panel._banked_line.cget("text")

    def test_nothing_to_reset_shows_why_instead(self, panel):
        show(panel, openai=codex_snap(applicable=0), engine=Engine.CODEX)
        assert not use_visible(panel)
        assert panel._banked_hint.cget("text") == "nothing to reset now"

    def test_a_codex_credit_that_would_reset_something_shows_use(self, panel):
        show(panel, openai=codex_snap(applicable=1), engine=Engine.CODEX)
        assert use_visible(panel)

    def test_a_paused_grant_is_not_offered(self, panel):
        show(panel, claude=claude_snap(banked=claude_banked(paused=True)))
        assert not use_visible(panel)

    def test_an_ineligible_account_has_no_block_at_all(self, panel):
        ineligible = br.parse_claude_banked({"eligible": False, "ineligible_reason": "surface"}, NOW)
        show(panel, claude=claude_snap(banked=ineligible))
        assert not panel._banked_frame.winfo_manager()

    def test_deepseek_has_no_block(self, panel):
        from models import DeepSeekSnapshot
        panel._active_engine = Engine.DEEPSEEK
        panel.update_combined(CombinedSnapshot(deepseek=DeepSeekSnapshot(balance_available=True,
                                                                         balance_total=5.0)))
        panel._apply_snapshot_update()
        assert not panel._banked_frame.winfo_manager()


class TestNothingIsSentWithoutConfirmation:
    def test_pressing_use_only_opens_the_confirmation(self, panel):
        show(panel, claude=claude_snap())
        panel._on_use_reset()
        assert len(StubDialog.shown) == 1
        assert panel.runner.attempts == []

    def test_the_confirmation_names_the_reset_and_the_usage(self, panel):
        show(panel, claude=claude_snap())
        panel._on_use_reset()
        lines = StubDialog.shown[0].lines
        assert lines[0] == "Opus launch reset"
        assert any(line.startswith("Expires") for line in lines)
        assert "Clears the 5-hour limit and the weekly limit" in lines
        assert "Current usage: session 12%, weekly 39%" in lines
        assert lines[-1] == "This uses the reset now and cannot be undone."

    def test_the_codex_confirmation_uses_the_window_it_has(self, panel):
        show(panel, openai=codex_snap(), engine=Engine.CODEX)
        panel._on_use_reset()
        assert "Current usage: 30-day 4%" in StubDialog.shown[0].lines

    def test_cancelling_sends_nothing(self, panel):
        show(panel, claude=claude_snap())
        panel._on_use_reset()
        StubDialog.shown[0].on_cancel()
        assert panel.runner.attempts == []
        assert panel._redeem_flow.state == "idle"

    def test_use_on_cached_data_does_nothing(self, panel):
        show(panel, claude=claude_snap(stale=True))
        panel._on_use_reset()
        assert StubDialog.shown == [] and panel.runner.attempts == []


class TestConfirming:
    def confirm(self, panel):
        panel._on_use_reset()
        StubDialog.shown[-1].on_confirm()

    def test_one_confirmation_sends_exactly_one_request(self, panel):
        show(panel, claude=claude_snap())
        self.confirm(panel)
        finish(panel)
        assert len(panel.runner.attempts) == 1
        attempt = panel.runner.attempts[0]
        assert attempt.provider == "claude"
        assert attempt.claim.credit_id == "opus55-launch-promax-20260921"

    def test_a_double_confirmation_still_sends_one(self, panel):
        show(panel, claude=claude_snap())
        panel._on_use_reset()
        dialog = StubDialog.shown[-1]
        dialog.on_confirm()
        dialog.on_confirm()
        finish(panel)
        assert len(panel.runner.attempts) == 1

    def test_use_is_disabled_while_the_request_is_in_flight(self, panel):
        panel.runner.gate = threading.Event()
        show(panel, claude=claude_snap())
        self.confirm(panel)
        panel._refresh_display_immediate()
        assert use_visible(panel)
        assert str(panel._banked_use.cget("state")) == "disabled"
        panel._on_use_reset()                                # a second press while waiting
        assert len(StubDialog.shown) == 1
        panel.runner.gate.set()
        finish(panel)
        assert len(panel.runner.attempts) == 1

    def test_a_claim_that_changed_behind_the_dialog_sends_nothing(self, panel):
        show(panel, claude=claude_snap())
        panel._on_use_reset()
        show(panel, claude=claude_snap(stale=True))          # the data went stale meanwhile
        StubDialog.shown[-1].on_confirm()
        assert panel.runner.attempts == []
        assert "changed" in panel._banked_notice_text()

    def test_success_says_so_and_refreshes_usage(self, panel):
        show(panel, claude=claude_snap())
        self.confirm(panel)
        finish(panel)
        assert panel._banked_notice_text() == "Reset applied · 2 limits refreshed"
        assert panel.refreshes == [1]
        assert panel._redeem_flow.state == "done"

    def test_a_refusal_says_so_and_still_refreshes(self, panel):
        panel.runner.outcome = rr.RedeemOutcome("nothing", "Nothing to reset right now · the reset stays available", 0, False)
        show(panel, claude=claude_snap())
        self.confirm(panel)
        finish(panel)
        assert "stays available" in panel._banked_notice_text()
        assert panel.refreshes == [1]

    def test_an_unknown_result_is_never_retried_automatically(self, panel):
        panel.runner.outcome = rr.UNKNOWN
        show(panel, claude=claude_snap())
        self.confirm(panel)
        finish(panel)
        assert len(panel.runner.attempts) == 1
        assert panel._banked_notice_text() == "Result unknown · refresh before trying again"
        assert panel._redeem_flow.state == "unknown"

    def test_a_retry_the_user_chooses_after_an_unknown_result_reuses_the_key(self, panel):
        panel.runner.outcome = rr.UNKNOWN
        show(panel, claude=claude_snap(offset=-5))
        self.confirm(panel)
        finish(panel)
        show(panel, claude=claude_snap(offset=5))             # the refresh the notice asked for
        self.confirm(panel)
        finish(panel)
        first, second = panel.runner.attempts
        assert first.request_id == second.request_id

    def test_a_new_attempt_after_a_definite_result_has_a_new_key(self, panel):
        panel.runner.outcome = rr.RedeemOutcome("nothing", "nothing", 0, False)
        show(panel, claude=claude_snap(offset=-5))
        self.confirm(panel)
        finish(panel)
        show(panel, claude=claude_snap(offset=5))
        self.confirm(panel)
        finish(panel)
        first, second = panel.runner.attempts
        assert first.request_id != second.request_id

    def test_a_crash_in_the_worker_reads_as_unknown_not_as_a_leak(self, panel):
        def boom(attempt):
            raise RuntimeError("token=SECRET-TOKEN body=SECRET-BODY")
        panel._run_attempt = boom
        show(panel, claude=claude_snap())
        self.confirm(panel)
        finish(panel)
        text = panel._banked_notice_text()
        assert text == "Result unknown · refresh before trying again"
        assert "SECRET" not in text

    def test_a_display_failure_after_confirming_does_not_strand_the_flow(self, panel):
        """Once confirmed the flow is "sending": the request has to go out and the
        result has to be applied even if repainting the panel fails right then."""
        show(panel, claude=claude_snap())
        panel._on_use_reset()
        real = panel._refresh_display_immediate
        calls = []

        def fails_once():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("tk is unhappy")
            real()
        panel._refresh_display_immediate = fails_once
        try:
            StubDialog.shown[-1].on_confirm()
        except RuntimeError:
            pass
        finish(panel)
        assert len(panel.runner.attempts) == 1
        assert panel._redeem_flow.state == "done" and not panel._redeem_flow.in_flight

    def test_a_worker_that_cannot_start_sends_nothing_and_frees_the_flow(self, panel, monkeypatch):
        def cannot(self):
            raise RuntimeError("can't start new thread")
        monkeypatch.setattr(threading.Thread, "start", cannot)
        show(panel, claude=claude_snap())
        self.confirm(panel)
        assert panel.runner.attempts == []
        assert not panel._redeem_flow.in_flight
        assert panel._redeem_flow.state == "done"
        assert panel._banked_notice_text() == "Couldn't send · nothing was sent"

    def test_the_notice_clears_itself(self, panel):
        show(panel, claude=claude_snap())
        self.confirm(panel)
        finish(panel)
        panel._expire_notice()
        assert panel._banked_notice_text() == ""


class TestNoSecondSpendBeforeTheRefreshLands:
    """After any result the old snapshot still says the reset is there. Use stays
    away until a snapshot collected after the result arrives."""

    def spend_once(self, panel):
        panel._on_use_reset()
        StubDialog.shown[-1].on_confirm()
        finish(panel)

    def test_use_is_hidden_after_a_result(self, panel):
        show(panel, claude=claude_snap(offset=-5))
        self.spend_once(panel)
        panel._refresh_display_immediate()
        assert not use_visible(panel)
        assert panel._banked_claim() is None

    def test_pressing_use_again_does_nothing_until_then(self, panel):
        show(panel, claude=claude_snap(offset=-5))
        self.spend_once(panel)
        panel._on_use_reset()
        assert len(StubDialog.shown) == 1 and len(panel.runner.attempts) == 1

    def test_the_same_old_snapshot_shown_again_stays_blocked(self, panel):
        old = claude_snap(offset=-5)
        show(panel, claude=old)
        self.spend_once(panel)
        show(panel, claude=old)
        assert not use_visible(panel)

    def test_a_snapshot_collected_after_the_result_brings_use_back(self, panel):
        show(panel, claude=claude_snap(offset=-5))
        self.spend_once(panel)
        show(panel, claude=claude_snap(offset=5))
        assert use_visible(panel)

    def test_a_refreshed_snapshot_without_the_reset_offers_nothing(self, panel):
        show(panel, claude=claude_snap(offset=-5))
        self.spend_once(panel)
        show(panel, claude=claude_snap(banked=None, offset=5))
        assert not use_visible(panel)

    def test_an_unknown_result_also_waits_for_a_refresh(self, panel):
        panel.runner.outcome = rr.UNKNOWN
        show(panel, claude=claude_snap(offset=-5))
        self.spend_once(panel)
        panel._refresh_display_immediate()
        assert not use_visible(panel)
        show(panel, claude=claude_snap(offset=5))
        assert use_visible(panel)

    def test_a_cached_snapshot_after_the_result_still_offers_nothing(self, panel):
        show(panel, claude=claude_snap(offset=-5))
        self.spend_once(panel)
        show(panel, claude=claude_snap(stale=True, offset=5))
        assert not use_visible(panel)


class TestTheUpdateLoopSurvivesAFailure:
    """One step of the tick failing must not stop the tick from running again or
    keep a finished redeem result from being applied."""

    def test_a_failing_snapshot_update_does_not_stop_the_loop_or_the_redeem_result(
            self, panel, monkeypatch):
        show(panel, claude=claude_snap())
        panel._on_use_reset()
        StubDialog.shown[-1].on_confirm()
        panel._redeem_thread.join(5)                          # the result is waiting to be applied

        def failing():
            raise ValueError("a view that cannot be drawn")
        panel._apply_snapshot_update = failing
        scheduled = []
        monkeypatch.setattr(panel._window.master, "after", lambda ms, fn: scheduled.append(ms))

        panel._update_tick()

        assert scheduled == [50]                              # the next tick is still booked
        assert panel._redeem_flow.state == "done"             # and the result was still applied


class TestTheWindowFollowsTheBankedBlock:
    """Anything that changes the block's height makes the panel measure itself again."""

    def watch_resizes(self, panel):
        resized = []
        real = panel._update_window_size
        panel._update_window_size = lambda: (resized.append(1), real())
        return resized

    def test_the_reset_line_going_while_a_result_stays_resizes(self, panel):
        show(panel, claude=claude_snap(offset=-5))
        panel._on_use_reset()
        StubDialog.shown[-1].on_confirm()
        finish(panel)                                         # the result notice is up
        resized = self.watch_resizes(panel)
        show(panel, claude=claude_snap(banked=None, offset=5))   # the last reset is gone
        assert resized

    def test_a_longer_notice_resizes(self, panel):
        show(panel, claude=claude_snap())
        panel._set_notice("Reset applied", "ok", "claude")
        resized = self.watch_resizes(panel)
        panel._set_notice("Couldn't send, and there is a good deal more to say about it than "
                          "fits on one line of the panel", "warn", "claude")
        assert resized

    def test_use_appearing_or_going_resizes(self, panel):
        show(panel, claude=claude_snap())
        assert use_visible(panel)
        resized = self.watch_resizes(panel)
        show(panel, claude=claude_snap(stale=True))           # the data went stale: no Use
        assert not use_visible(panel)
        assert resized

    def test_an_unchanged_block_does_not_resize_again(self, panel):
        show(panel, claude=claude_snap())
        resized = self.watch_resizes(panel)
        show(panel, claude=claude_snap())
        assert not resized


class TestTheRealConfirmationDialog:
    """The dialog itself (the panel tests stub it): a click that lands within the
    first moments, as the second click of a double-click on Use would, confirms
    nothing."""

    def open(self, tk_root, on_confirm, on_cancel):
        style = SimpleNamespace(
            bg="#1c1c1e", primary="#fff", secondary="#aaa", muted="#777", accent="#F59E0B",
            warn="#f00", button_bg="#333", hover_bg="#444",
            font=lambda size, weight="normal": ("Segoe UI", size, weight))
        return ui_window.ResetConfirmDialog(
            tk_root, ["Opus launch reset", "Expires Oct 22", "This cannot be undone."],
            on_confirm, on_cancel, style)

    def test_an_immediate_click_on_use_reset_confirms_nothing(self, tk_root):
        confirmed, cancelled = [], []
        dialog = self.open(tk_root, lambda: confirmed.append(1), lambda: cancelled.append(1))
        try:
            dialog.use_button.invoke()
            assert confirmed == [] and cancelled == []
        finally:
            dialog._cancel()

    def test_a_deliberate_click_confirms_once(self, tk_root):
        confirmed, cancelled = [], []
        dialog = self.open(tk_root, lambda: confirmed.append(1), lambda: cancelled.append(1))
        dialog._armed_at = 0.0                               # the moment has passed
        dialog.use_button.invoke()
        dialog._confirm()                                    # the dialog is gone: no second call
        assert confirmed == [1] and cancelled == []

    def test_cancel_works_at_once(self, tk_root):
        confirmed, cancelled = [], []
        dialog = self.open(tk_root, lambda: confirmed.append(1), lambda: cancelled.append(1))
        dialog.cancel_button.invoke()
        assert confirmed == [] and cancelled == [1]
