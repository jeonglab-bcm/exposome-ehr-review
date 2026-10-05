"""Rate-limited, retrying HTTP client built on urllib.

Every external service this project touches publishes a rate limit or asks for
identification. The client therefore enforces a minimum interval between
requests per host and identifies itself with a User-Agent, rather than relying
on the caller to remember.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import USER_AGENT

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    """A request failed after exhausting retries."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class RateLimitedClient:
    """Serial HTTP client with a per-host minimum request interval.

    Args:
        min_interval: seconds to wait between requests to the same host.
        max_retries: attempts after the first before giving up.
        timeout: per-request socket timeout in seconds.
    """

    min_interval: float = 0.34
    max_retries: int = 4
    timeout: float = 60.0
    backoff_base: float = 2.0
    _last_call: dict[str, float] = field(default_factory=dict, repr=False)
    request_count: int = field(default=0, repr=False)

    def _throttle(self, host: str) -> None:
        last = self._last_call.get(host)
        if last is not None:
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[host] = time.monotonic()

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        return self.request("GET", url, params=params, headers=headers)

    def request(
        self,
        method: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        if params:
            flat = {k: v for k, v in params.items() if v is not None}
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(flat)
        body = urllib.parse.urlencode(data).encode() if data else None
        host = urllib.parse.urlparse(url).netloc

        hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        if headers:
            hdrs.update(headers)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    self.request_count += 1
                    return raw
            except urllib.error.HTTPError as exc:  # noqa: PERF203
                detail = ""
                try:
                    detail = exc.read()[:500].decode("utf-8", "replace")
                except Exception:  # pragma: no cover - best effort only
                    pass
                if exc.code not in RETRY_STATUS or attempt == self.max_retries:
                    raise HttpError(
                        f"{method} {url} failed with HTTP {exc.code}", exc.code, detail
                    ) from exc
                last_exc = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt == self.max_retries:
                    raise HttpError(f"{method} {url} failed: {exc}") from exc
                last_exc = exc

            sleep_for = self.backoff_base**attempt + random.uniform(0, 0.5)
            log.warning(
                "retry %d/%d for %s after %s (sleeping %.1fs)",
                attempt + 1,
                self.max_retries,
                url.split("?")[0],
                last_exc,
                sleep_for,
            )
            time.sleep(sleep_for)

        raise HttpError(f"{method} {url} exhausted retries")  # pragma: no cover

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        hdrs = {"Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        return json.loads(self.get(url, params=params, headers=hdrs).decode("utf-8"))

    def post_json(
        self,
        url: str,
        data: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """POST a form and parse JSON. esearch takes its term in the body, so a long
        query is not refused as an over-long URL (HTTP 414)."""
        hdrs = {"Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        return json.loads(self.request("POST", url, data=data, headers=hdrs).decode("utf-8"))
