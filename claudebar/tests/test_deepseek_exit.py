"""Choosing Exit during a DeepSeek fetch must not wait for the HTTP timeout.

A ThreadPoolExecutor's workers are not daemons and the interpreter joins them when it
exits, so a refresh in flight held the process for up to the request timeout. The two
month requests run on daemon threads instead, and still run at the same time.
"""

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import deepseek_usage as d

SRC = str(Path(d.__file__).resolve().parent)


def test_the_two_requests_run_at_the_same_time(monkeypatch):
    both_in_flight = threading.Barrier(2, timeout=5)     # breaks if they ran one after the other
    seen = []

    def fake_get(url, headers):
        both_in_flight.wait()
        seen.append(url)
        return {"data": {}}, None

    monkeypatch.setattr(d, "_get", fake_get)
    amount, cost, error = d.fetch_month("token", 2026, 11)
    assert error is None and amount == cost == {"data": {}}
    assert sorted(seen) == [f"{d.USAGE_AMOUNT_URL}?month=11&year=2026",
                            f"{d.USAGE_COST_URL}?month=11&year=2026"]


def test_the_requests_run_on_daemon_threads(monkeypatch):
    daemons = []

    def fake_get(url, headers):
        daemons.append(threading.current_thread().daemon)
        return {}, None

    monkeypatch.setattr(d, "_get", fake_get)
    d.fetch_month("token", 2026, 11)
    assert daemons == [True, True]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_worker_that_dies_leaves_an_error_not_a_crash(monkeypatch):
    def fake_get(url, headers):
        if "amount" in url:
            raise SystemExit              # not an Exception: _get's own handler does not catch it
        return {"data": {}}, None

    monkeypatch.setattr(d, "_get", fake_get)
    amount, cost, error = d.fetch_month("token", 2026, 11)
    assert amount is None and error == "Request failed"


def test_a_process_with_a_fetch_in_flight_exits_at_once():
    """The real thing: the interpreter ends while both requests are still waiting."""
    script = textwrap.dedent("""
        import sys, threading, time
        sys.path.insert(0, sys.argv[1])
        import deepseek_usage as d

        def never_answers(url, headers):
            time.sleep(20)
            return None, "late"

        d._get = never_answers
        threading.Thread(target=d.fetch_month, args=("token", 2026, 11), daemon=True).start()
        time.sleep(0.5)          # both requests are in flight; now the main thread ends, as Exit does
    """)
    done = subprocess.run([sys.executable, "-c", script, SRC], timeout=10, capture_output=True)
    assert done.returncode == 0
