"""The HTTP calls ClaudeBar makes: ones that carry a token, and two that do not.

urllib's default opener follows a 301, 302 or 303 by sending the request again, with
every header and so the bearer token, to whatever host the `Location` header names, and
it turns a POST into a GET on the way. A redirect from an authenticated endpoint could
therefore hand the token to another host, and a request that spends a banked reset
would be replaced by a different one before the caller ever saw a status.

So every call follows no redirect unless its caller says it may: a 3xx comes back as the
`urllib.error.HTTPError` of that status, like any other unexpected answer. Only the two
public downloads that carry no credentials (exchange rates, the price catalog) ask to
follow redirects.
"""

import json
import time
import urllib.request
from typing import Any, Callable, Optional


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back to the caller as an HTTPError instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def build_request(url: str, headers: Optional[dict] = None, method: str = "GET",
                  body: Optional[dict] = None) -> urllib.request.Request:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    return urllib.request.Request(url, data=data, headers=headers or {}, method=method)


def open_url(request: urllib.request.Request, timeout: float, follow_redirects: bool = False):
    """The one place a request leaves the app. A context manager whose `read()` is the
    body; a non-2xx answer, a redirect included unless asked for, raises HTTPError."""
    if follow_redirects:
        return urllib.request.urlopen(request, timeout=timeout)
    return _OPENER.open(request, timeout=timeout)


def request_json(url: str, headers: Optional[dict], timeout: float, method: str = "GET",
                 body: Optional[dict] = None, follow_redirects: bool = False) -> Any:
    """Send one request and return the parsed JSON of the answer.

    Raises HTTPError for any status outside 2xx, URLError when there was no answer, and
    ValueError (JSONDecodeError, UnicodeDecodeError) for a body that is not JSON."""
    with open_url(build_request(url, headers, method, body), timeout, follow_redirects) as response:
        return json.loads(response.read().decode("utf-8"))


class FailureMemory:
    """Remembers that a download failed, so a host that is down is asked again after
    `seconds` instead of at every refresh (each try can cost its whole timeout)."""

    def __init__(self, seconds: float = 600.0, clock: Callable[[], float] = time.monotonic):
        self._seconds = seconds
        self._clock = clock
        self._failed_at: Optional[float] = None

    def blocked(self) -> bool:
        """True while a recent failure says not to ask yet."""
        return self._failed_at is not None and self._clock() - self._failed_at < self._seconds

    def failed(self) -> None:
        self._failed_at = self._clock()

    def clear(self) -> None:
        self._failed_at = None
