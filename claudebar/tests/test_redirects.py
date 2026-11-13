"""A call that carries a token never follows a redirect.

urllib's default opener answers a 301, 302 or 303 by sending the request again, with
every header, to the host the Location names. These tests use real urllib against two
little servers on this machine: an "origin" that answers every request with a redirect
and an "other host" that records whatever reaches it. Nothing leaves the machine.
"""

import http.server
import threading
import urllib.error
import urllib.request

import pytest

import banked_resets as br
import deepseek_usage
import http_json
import oauth_usage
import openai_usage
import reset_redeem as rr

CODES = (301, 302, 303, 307, 308)
TOKEN = "tok-SECRET-123"


class _Server:
    """A loopback HTTP server running in a thread. `answer(handler)` writes the reply."""

    def __init__(self, answer):
        self.requests = []          # (method, path, headers) of everything that reached it
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                server.requests.append((self.command, self.path, dict(self.headers)))
                answer(self)

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def _ok(handler):
    body = b'{"result": "success", "rates": {}}'
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@pytest.fixture
def hosts(monkeypatch):
    """(origin, other): origin redirects to `other` with the status in `origin.status`."""
    other = _Server(_ok)
    origin = _Server(lambda handler: _redirect(handler, origin.status, other.url + "/stolen"))
    origin.status = 302

    def _redirect(handler, code, location):
        handler.send_response(code)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    # A system proxy must not stand between the test and the loopback servers.
    no_proxy = urllib.request.ProxyHandler({})
    monkeypatch.setattr(http_json, "_OPENER", urllib.request.build_opener(http_json._NoRedirect, no_proxy))
    monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener(no_proxy))
    yield origin, other
    origin.close()
    other.close()


@pytest.mark.parametrize("code", CODES)
class TestAuthenticatedCallsDoNotFollowRedirects:
    def test_the_claude_usage_call(self, hosts, monkeypatch, code):
        origin, other = hosts
        origin.status = code
        monkeypatch.setattr(oauth_usage, "USAGE_URL", origin.url + "/usage")
        data = oauth_usage.fetch_oauth_usage(access_token=TOKEN)
        assert other.requests == []
        assert len(origin.requests) == 1                    # and no retry
        assert not data.is_valid and data.error.startswith(f"HTTP {code}")

    def test_the_codex_usage_call(self, hosts, monkeypatch, code):
        origin, other = hosts
        origin.status = code
        monkeypatch.setattr(openai_usage, "USAGE_URL", origin.url + "/usage")
        data = openai_usage.fetch_openai_usage(access_token=TOKEN)
        assert other.requests == []
        assert len(origin.requests) == 1
        assert not data.is_valid and data.error.startswith(f"HTTP {code}")

    def test_the_codex_credit_details_call(self, hosts, monkeypatch, code):
        origin, other = hosts
        origin.status = code
        monkeypatch.setattr(openai_usage, "RESET_CREDITS_URL", origin.url + "/credits")
        assert openai_usage.fetch_reset_credits(TOKEN, "acct-1") is None
        assert other.requests == []
        assert len(origin.requests) == 1

    def test_a_deepseek_call(self, hosts, code):
        origin, other = hosts
        origin.status = code
        payload, error = deepseek_usage._get(origin.url + "/summary", {"Authorization": f"Bearer {TOKEN}"})
        assert payload is None and error == f"HTTP {code}"
        assert other.requests == []
        assert len(origin.requests) == 1

    def test_the_deepseek_summary(self, hosts, monkeypatch, code):
        origin, other = hosts
        origin.status = code
        monkeypatch.setattr(deepseek_usage, "SUMMARY_URL", origin.url + "/summary")
        assert deepseek_usage.fetch_summary(TOKEN).error == f"HTTP {code}"
        assert other.requests == []

    def test_the_redeem_transport_hands_the_status_back(self, hosts, code):
        origin, other = hosts
        origin.status = code
        result = rr.UrllibTransport().request("POST", origin.url + "/consume",
                                              {"Authorization": f"Bearer {TOKEN}"},
                                              {"redeem_request_id": "req-1"}, 5)
        assert result.status == code
        assert other.requests == []
        assert [r[0] for r in origin.requests] == ["POST"]     # a POST stays a POST, once

    def test_a_redirected_codex_spend_is_unknown(self, hosts, monkeypatch, code):
        origin, other = hosts
        origin.status = code
        monkeypatch.setattr(rr, "CODEX_CONSUME_URL", origin.url + "/consume")
        monkeypatch.setattr(openai_usage, "load_codex_token", lambda: TOKEN)
        monkeypatch.setattr(openai_usage, "load_codex_account_id", lambda: "acct-1")
        outcome = rr.run_attempt(rr.Attempt(br.Claim("codex", "c-1", "x", None), "req-1"))
        assert outcome.kind == "unknown" and outcome.consumed is None
        assert other.requests == []
        assert len(origin.requests) == 1

    def test_a_redirected_claude_spend_is_unknown(self, hosts, monkeypatch, code):
        origin, other = hosts
        origin.status = code
        monkeypatch.setattr(rr, "CLAUDE_RESET_URL", origin.url + "/orgs/{org}/reset")
        profile = rr.HttpResult(200, {"organization": {"uuid": "org-1"}})
        real = rr.UrllibTransport()

        class ProfileFakedPostReal:
            def request(self, method, url, headers, body, timeout):
                if method == "GET":
                    return profile                      # the read is faked, the spend is real urllib
                return real.request(method, url, headers, body, timeout)

        claim = br.Claim("claude", "grant-1", "x", None)
        outcome = rr.redeem_claude(ProfileFakedPostReal(), TOKEN, claim, "req-1")
        assert outcome.kind == "unknown" and outcome.consumed is None
        assert other.requests == []
        assert [r[0] for r in origin.requests] == ["POST"]


@pytest.mark.parametrize("code", CODES)
class TestTheReadOfARedirectedProfileSendsNothing:
    def test_the_profile_read(self, hosts, monkeypatch, code):
        origin, other = hosts
        origin.status = code
        monkeypatch.setattr(rr, "PROFILE_URL", origin.url + "/profile")
        claim = br.Claim("claude", "grant-1", "x", None)
        outcome = rr.redeem_claude(rr.UrllibTransport(), TOKEN, claim, "req-1")
        assert outcome.consumed is False                # nothing was sent: the POST never started
        assert other.requests == []
        assert [r[0] for r in origin.requests] == ["GET"]


class TestTheModuleItself:
    def test_a_redirect_is_not_followed_by_default(self, hosts):
        origin, other = hosts
        with pytest.raises(urllib.error.HTTPError) as caught:
            http_json.request_json(origin.url, {"Authorization": f"Bearer {TOKEN}"}, 5)
        assert caught.value.code == 302
        assert other.requests == []

    def test_the_two_public_downloads_may_follow_one(self, hosts):
        origin, other = hosts
        data = http_json.request_json(origin.url, {"User-Agent": "x"}, 5, follow_redirects=True)
        assert data["result"] == "success"
        assert [r[1] for r in other.requests] == ["/stolen"]

    def test_the_devtools_listing_does_not_follow_one_either(self, hosts):
        import browser_cdp
        origin, other = hosts
        assert browser_cdp._http_json(origin.url + "/json/list") is None
        assert other.requests == []

    def test_the_body_and_method_are_sent_as_given(self, hosts):
        origin, other = hosts
        origin.status = 308
        with pytest.raises(urllib.error.HTTPError):
            http_json.request_json(origin.url, {"X-Test": "1"}, 5, method="POST", body={"a": 1})
        method, _, headers = origin.requests[0]
        assert method == "POST" and headers["X-Test"] == "1"
        assert headers["Content-Length"] == str(len(b'{"a": 1}'))


class TestFailureMemory:
    def test_it_blocks_for_the_window_and_then_lets_a_try_through(self):
        now = [100.0]
        memory = http_json.FailureMemory(seconds=600, clock=lambda: now[0])
        assert not memory.blocked()                  # nothing has failed yet
        memory.failed()
        now[0] += 599
        assert memory.blocked()
        now[0] += 2
        assert not memory.blocked()

    def test_clear_forgets_a_failure(self):
        memory = http_json.FailureMemory()
        memory.failed()
        memory.clear()
        assert not memory.blocked()
