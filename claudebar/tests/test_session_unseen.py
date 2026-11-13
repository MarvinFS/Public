"""A cached reading that shows no session running, and what the logs say since: when a
request was made after it, a session is in use that the reading cannot show."""

import os
from datetime import datetime, timedelta, timezone

import pytest

from log_parser import last_log_write
from models import UsageSnapshot

NOW = datetime(2026, 10, 17, 16, 53, 0, tzinfo=timezone.utc)
UTC = timezone.utc


def cached(**fields):
    return UsageSnapshot(is_stale=True, **fields)


class TestWhenAReadingLastShowedNoSession:
    def test_a_reading_with_no_session_begun_says_so_as_of_its_own_time(self):
        taken = datetime(2026, 10, 17, 16, 39).astimezone()      # naive local, as the cache holds it
        snap = cached(session_percent=0.0, session_resets_at=None, stale_since=taken.replace(tzinfo=None))
        assert snap.session_unseen_since(NOW) == taken

    def test_a_reading_whose_session_is_over_says_so_as_of_the_reset(self):
        reset = NOW - timedelta(minutes=50)
        snap = cached(session_percent=59.0, session_resets_at=reset, stale_since=reset - timedelta(minutes=8))
        assert snap.session_unseen_since(NOW) == reset

    def test_a_session_still_ahead_of_its_reset_is_seen(self):
        snap = cached(session_percent=59.0, session_resets_at=NOW + timedelta(hours=1), stale_since=datetime.now())
        assert snap.session_unseen_since(NOW) is None

    def test_a_live_reading_is_never_unseen(self):
        snap = UsageSnapshot(session_percent=0.0, session_resets_at=None)
        assert snap.session_unseen_since(NOW) is None
        snap.session_resets_at = NOW - timedelta(minutes=5)
        assert snap.session_unseen_since(NOW) is None

    def test_a_figure_with_no_reset_time_is_words_only_not_a_missing_session(self):
        snap = cached(session_percent=40.0, session_resets_at=None, session_reset="in about an hour",
                      stale_since=datetime.now())
        assert snap.session_unseen_since(NOW) is None

    def test_a_cached_reading_with_no_time_cannot_be_compared(self):
        assert cached(session_percent=0.0, session_resets_at=None).session_unseen_since(NOW) is None


class TestLastLogWrite:
    def test_it_is_the_newest_file_time_in_any_project(self, tmp_path):
        old, new = tmp_path / "a" / "old.jsonl", tmp_path / "b" / "new.jsonl"
        for path, stamp in ((old, 1_700_000_000), (new, 1_800_000_000)):
            path.parent.mkdir()
            path.write_text("{}\n")
            os.utime(path, (stamp, stamp))
        assert last_log_write(tmp_path) == datetime.fromtimestamp(1_800_000_000).astimezone()

    @pytest.mark.parametrize("make", [lambda p: p, lambda p: p / "missing"])
    def test_no_logs_is_none(self, tmp_path, make):
        assert last_log_write(make(tmp_path)) is None

    def test_other_files_do_not_count(self, tmp_path):
        (tmp_path / "notes.txt").write_text("x")
        assert last_log_write(tmp_path) is None
