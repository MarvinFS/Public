"""The rule: no fact is shown twice on the panel at the same time.

What sits above the tabs (the limit bars, the banked-resets block, the DeepSeek balance, the header's time
and its "(cached)") is what stays when the panel shrinks, so it wins, and the Quota tab shows only what it
does not: the burn rate, the clock times, the gain of a reset. The panel is real (a window on the test run's
own desktop); only the snapshots are made here, and nothing is sent anywhere."""

import re
from datetime import datetime, timedelta, timezone

import pytest

import banked_resets as br
import quota_history
import ui_window
from config import Config
from models import CombinedSnapshot, DeepSeekSnapshot, Engine, OpenAISnapshot, UsageSnapshot
from tests.test_panel_scale import STANDARD
from ui_scale import DROP_ORDER

UTC = timezone.utc


def h(hours):
    return timedelta(hours=hours)


@pytest.fixture
def panel(tk_root, monkeypatch):
    monkeypatch.setattr(ui_window, "get_exchange_rates", lambda: None)
    monkeypatch.setattr(ui_window, "check_claude_status", lambda: None)
    monkeypatch.setattr(ui_window, "save_config", lambda cfg: None)
    monkeypatch.setattr(ui_window, "monitor_at", lambda x, y, nearest=True: STANDARD)
    config = Config()
    config.panel_view = "forecast"
    window = ui_window.ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None,
                                       on_exit=lambda: None, config=config,
                                       on_engine_change=lambda e: None)
    window._create_window()
    assert window.scale.k == 1.0
    yield window
    window.destroy()


def show(window, engine, **snapshots):
    window._active_engine = engine
    window._is_collecting = False
    window.update_combined(CombinedSnapshot(**snapshots))
    window._apply_snapshot_update()
    window._window.deiconify()
    window._window.update()


def record(key, resets_at, points):
    """Readings the panel's history holds: (minutes ago, percent left) pairs."""
    now = datetime.now(UTC)
    for minutes, remaining in points:
        quota_history.append(key, remaining, resets_at, now=now - timedelta(minutes=minutes))


def claude_banked(now):
    grant = {"id": "g1", "label": "Launch reset", "resets_total": 3, "resets_left": 3,
             "ends_at": (now + timedelta(days=29)).isoformat(), "clears": [], "paused": False,
             "usable_now": True, "use_requires_limit": False}
    return br.parse_claude_banked({"eligible": True, "next_grant_id": "g1", "grants": [grant]}, now)


def codex_banked(now):
    return br.parse_codex_banked(
        {"available_count": 3, "applicable_available_count": 1},
        {"credits": [{"id": "c1", "status": "available", "reset_type": "codex_rate_limits",
                      "title": "Full reset (Monthly)",
                      "expires_at": (now + timedelta(days=20)).isoformat()}]}, now)


def top_texts(panel) -> list:
    """Every text the blocks above the tabs show."""
    texts = []
    for bar in panel._limits.values():
        if bar["frame"].winfo_manager():
            texts += [bar[cell].cget("text") for cell in ("reset", "left", "note", "pace")]
    if panel._banked_frame.winfo_manager():
        texts.append(panel._banked_line.cget("text"))
    if panel._balance_frame.winfo_manager():
        texts += [panel._balance_value.cget("text"), panel._balance_state.cget("text")]
    texts += [panel._updated_label.cget("text"), panel._stale_label.cget("text")]
    return [text for text in texts if text]


def tab_texts(panel) -> list:
    """Every text the Quota tab shows: the headline, its caption and the detail rows."""
    texts = [panel._fc_value.cget("text"), panel._fc_caption.cget("text")]
    for row, left, right in panel._fc_rows:
        if row.winfo_manager():
            texts += [left.cget("text"), right.cget("text")]
    return [text for text in texts if text]


def facts(texts) -> list:
    """The facts in a list of texts: each piece between the middle dots."""
    return [piece.strip() for text in texts for piece in text.split("\u00b7") if piece.strip()]


# Each scenario: what the top must show (so the test is not vacuous), what the tab must show,
# and words that must not be anywhere in the tab.
def claude_runs_out(panel):
    now = datetime.now(UTC)
    resets = now + h(2) + timedelta(minutes=28, seconds=30)
    snap = UsageSnapshot(timestamp=datetime.now(), session_percent=58.0, session_resets_at=resets,
                         weekly_percent=40.0, weekly_resets_at=now + h(60), banked=claude_banked(now))
    record("claude_session", resets, [(150, 100.0), (120, 90.0), (90, 78.0), (60, 60.0), (30, 51.0), (5, 43.0)])
    show(panel, Engine.CLAUDE, claude=snap)


def claude_lasts(panel):
    now = datetime.now(UTC)
    resets = now + h(2)
    snap = UsageSnapshot(timestamp=datetime.now(), session_percent=20.0, session_resets_at=resets,
                         weekly_percent=40.0, weekly_resets_at=now + h(60))
    record("claude_session", resets, [(180, 100.0), (120, 95.0), (60, 88.0), (10, 81.0)])
    show(panel, Engine.CLAUDE, claude=snap)


def claude_cached(panel):
    now = datetime.now(UTC)
    snap = UsageSnapshot(timestamp=datetime.now(), session_percent=30.0, session_resets_at=now + h(3),
                         weekly_percent=40.0, weekly_resets_at=now + h(60), banked=claude_banked(now),
                         is_stale=True, stale_since=datetime.now() - timedelta(minutes=14))
    show(panel, Engine.CLAUDE, claude=snap)


def codex_session(panel):
    now = datetime.now(UTC)
    resets = now + h(3)
    codex = OpenAISnapshot(session_available=True, session_percent=30.0, session_resets_at=resets,
                           weekly_percent=40.0, weekly_resets_at=now + h(100), available=True,
                           banked=codex_banked(now))
    record("codex_session", resets, [(120, 90.0), (60, 80.0), (10, 72.0)])
    show(panel, Engine.CODEX, openai=codex)


def codex_free_plan(panel):
    now = datetime.now(UTC)
    resets = now + timedelta(days=23)
    codex = OpenAISnapshot(session_available=False, weekly_percent=4.0, weekly_window_hours=720.0,
                           weekly_resets_at=resets, available=True)
    record("codex", resets, [(48 * 60, 99.0), (36 * 60, 98.0), (24 * 60, 97.0), (12 * 60, 96.0)])
    show(panel, Engine.CODEX, openai=codex)


def deepseek(panel, cost=0.4, stale=False):
    snap = DeepSeekSnapshot(balance_available=True, balance_usable=True, balance_total=12.40,
                            usage_available=True, daily_costs=[cost] * 31, is_stale=stale,
                            stale_since=datetime.now() - timedelta(minutes=14) if stale else None)
    show(panel, Engine.DEEPSEEK, deepseek=snap)


SCENARIOS = {
    # name: (build, top must hold, tab must hold)
    "claude_runs_out_with_banked": (claude_runs_out, ("% left", "Runs out in", "3 available", "next expires"),
                                    ("~", "Runs out ", "Next reset", "A banked reset would restore it")),
    "claude_lasts": (claude_lasts, ("% left", "Lasts until reset"), ("~", "unused at reset", "Next reset")),
    "claude_cached": (claude_cached, ("(cached)", "3 available"), ("~", "Next reset")),
    "codex_session_with_banked": (codex_session, ("% left", "3 available"), ("~", "Next reset")),
    "codex_free_30_day_plan": (codex_free_plan, ("% left", "Lasts until reset"), ("~", "30-day window", "Next reset")),
    "deepseek": (deepseek, ("$12.40",), ("~$0.40/day", "last 7 days", "Runs out")),
    "deepseek_cached": (lambda panel: deepseek(panel, stale=True), ("$12.40", "(cached)"),
                        ("~$0.40/day", "last 7 days")),
}

# Words the blocks above own: never in the tab, whatever the engine.
OWNED_ABOVE = ("% left", "Runs out in", "Lasts until reset", "available", "expires", "Banked", "cached",
               "Updated", "$12.40")


class TestTheQuotaTabSaysWhatTheBlocksAboveDoNot:
    @pytest.mark.parametrize("name", SCENARIOS)
    def test_no_fact_is_said_twice(self, panel, name):
        build, on_top, on_tab = SCENARIOS[name]
        build(panel)
        top, tab = top_texts(panel), tab_texts(panel)
        above, here = " | ".join(top), " | ".join(tab)
        for needle in on_top:                       # the premise: the blocks above do show these
            assert needle in above, (needle, top)
        for needle in on_tab:                       # and the tab is not empty for want of anything to say
            assert needle in here, (needle, tab)
        for fact in facts(top):
            assert fact not in here, (fact, tab)
        for word in OWNED_ABOVE:
            assert word not in here, (word, tab)

    def test_the_claude_headline_is_the_burn_rate(self, panel):
        claude_runs_out(panel)
        assert re.fullmatch(r"~\d+(\.\d+)?%/h", panel._fc_value.cget("text"))
        assert panel._fc_caption.cget("text") == "5-hour session"

    def test_the_claude_run_out_is_a_clock_time_on_the_tab_and_a_countdown_above(self, panel):
        claude_runs_out(panel)
        runs_out = [right.cget("text") for row, left, right in panel._fc_rows
                    if row.winfo_manager() and left.cget("text") == "At this rate"]
        assert len(runs_out) == 1 and re.fullmatch(r"Runs out [A-Z][a-z]{2} \d\d:\d\d", runs_out[0])
        assert re.fullmatch(r"Runs out in \d+h \d+m", panel._limits["session"]["note"].cget("text"))

    def test_the_banked_hint_stays(self, panel):
        claude_runs_out(panel)
        assert "A banked reset would restore it" in tab_texts(panel)

    def test_the_codex_headlines_name_their_window(self, panel):
        codex_session(panel)
        assert panel._fc_caption.cget("text") == "5-hour session"
        codex_free_plan(panel)
        assert panel._fc_caption.cget("text") == "30-day window"
        assert re.fullmatch(r"~[\d.]+%/h", panel._fc_value.cget("text"))

    def test_the_deepseek_headline_is_the_spend_rate(self, panel):
        deepseek(panel)
        assert (panel._fc_value.cget("text"), panel._fc_caption.cget("text")) == ("~$0.40/day", "last 7 days")
        assert [left.cget("text") for row, left, _ in panel._fc_rows if row.winfo_manager()] == ["Runs out"]

    def test_no_spend_says_so_in_the_headline_and_has_no_rows(self, panel):
        deepseek(panel, cost=0.0)
        assert (panel._fc_value.cget("text"), panel._fc_caption.cget("text")) == ("No spend", "last 7 days")
        assert tab_texts(panel) == ["No spend", "last 7 days"]

    def test_a_waiting_tab_has_a_dash_and_no_rows(self, panel):
        now = datetime.now(UTC)
        snap = UsageSnapshot(timestamp=datetime.now(), session_percent=58.0,
                             session_resets_at=now - h(0.1), weekly_percent=40.0,
                             weekly_resets_at=now + h(60), is_stale=True, stale_since=datetime.now())
        show(panel, Engine.CLAUDE, claude=snap)
        assert tab_texts(panel) == ["\u2014"]


class TestATabWithNoDetailRows:
    """An empty frame keeps the height of the rows it last held, so a tab with nothing to list
    takes its rows out of the layout."""

    def host_height(self, panel):
        host = panel._fc_rows_host
        return host.winfo_reqheight() + panel._pads[str(host)]

    def test_it_leaves_no_gap_under_the_chart(self, panel):
        deepseek(panel)
        with_rows, host = panel._main_frame.winfo_reqheight(), self.host_height(panel)
        assert panel._fc_rows_host.winfo_manager() and host > 0
        deepseek(panel, cost=0.0)
        assert panel._fc_rows_host.winfo_manager() == ""
        assert with_rows - panel._main_frame.winfo_reqheight() == host

    def test_a_waiting_tab_takes_its_rows_out_too(self, panel):
        claude_runs_out(panel)
        assert panel._fc_rows_host.winfo_manager()
        now = datetime.now(UTC)
        over = UsageSnapshot(timestamp=datetime.now(), session_percent=58.0, session_resets_at=now - h(0.1),
                             weekly_percent=40.0, weekly_resets_at=now + h(60), is_stale=True,
                             stale_since=datetime.now())
        show(panel, Engine.CLAUDE, claude=over)
        assert panel._fc_rows_host.winfo_manager() == "" and not panel._fc_rows_host.winfo_ismapped()

    def test_the_rows_come_back_with_the_next_reading(self, panel):
        deepseek(panel, cost=0.0)
        assert panel._fc_rows_host.winfo_manager() == ""
        deepseek(panel)
        assert panel._fc_rows_host.winfo_manager() and panel._fc_rows_host.winfo_ismapped()

    def test_the_fit_counts_the_rows_that_are_there_and_no_others(self, panel):
        deepseek(panel)
        smallest, full = panel._fit_range
        host = self.host_height(panel)
        deepseek(panel, cost=0.0)
        assert panel._fit_range == (smallest, full - host)

    def test_a_height_that_cannot_hold_the_rows_drops_them_and_a_taller_one_brings_them_back(self, panel):
        deepseek(panel)
        smallest, full = panel._fit_range
        panel.config.panel_max_height = full - 1
        panel._update_window_size()
        panel._window.update()
        assert "chart" in panel._fit_hidden and not panel._fc_rows_host.winfo_ismapped()
        panel.config.panel_max_height = full
        panel._update_window_size()
        panel._window.update()
        assert not panel._fit_hidden and panel._fc_rows_host.winfo_ismapped()

    def test_an_empty_tab_does_not_count_the_height_of_rows_it_no_longer_has(self, panel):
        """Dropping the rows frees nothing when there are none: a height a little under the panel's
        must take the whole lower half out, not stop at rows that are not there."""
        deepseek(panel)
        deepseek(panel, cost=0.0)
        _, full = panel._fit_range
        assert panel._window.winfo_height() == full
        panel.config.panel_max_height = full - 20
        panel._update_window_size()
        panel._window.update()
        assert "half" in panel._fit_hidden and panel._window.winfo_height() <= full - 20

    def test_an_empty_tab_under_the_smallest_height_drops_everything(self, panel):
        deepseek(panel, cost=0.0)
        smallest, _ = panel._fit_range
        panel.config.panel_max_height = smallest
        panel._update_window_size()
        panel._window.update()
        assert panel._fit_hidden == set(DROP_ORDER) and panel._fc_rows_host.winfo_manager() == ""
        assert panel._window.winfo_height() == smallest
