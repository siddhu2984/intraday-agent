"""HTTP access to NSE's public files and website APIs, with cookies, retries, politeness and a disk cache.

NSE's JSON APIs (www.nseindia.com/api/…) serve its website: they want a browser User-Agent, a Referer and, at
times, the cookies set by an HTML page. They are unofficial and may throttle or change; archive files
(nsearchives.nseindia.com) are plain downloads.
"""

from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
DEFAULT_REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"


class Fetcher:
    def __init__(self, referer: str = DEFAULT_REFERER, pause_s: float = 0.3):
        self.referer, self.pause_s = referer, pause_s
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.addheaders = [("User-Agent", USER_AGENT), ("Referer", referer), ("Accept", "*/*")]
        self._warmed = False

    def _warm(self) -> None:
        """Visit an HTML page once for cookies; the APIs often work without them, so failures are ignored."""
        if not self._warmed:
            self._warmed = True
            try:
                self.opener.open(self.referer, timeout=30).read()
            except OSError:
                pass

    def get(self, url: str, cache: Path | None = None, retries: int = 3, timeout: float = 60) -> bytes | None:
        """Bytes of url (None on 404). With `cache`, a file already on disk is returned without a request."""
        if cache is not None and cache.exists():
            return cache.read_bytes() or None
        data = b""
        for attempt in range(1, retries + 1):
            try:
                with self.opener.open(url, timeout=timeout) as response:
                    data = response.read()
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    break
                if attempt == retries:
                    raise
            except OSError:
                if attempt == retries:
                    raise
            time.sleep(5 * attempt)
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
        time.sleep(self.pause_s)  # be polite to NSE
        return data or None

    def api(self, url: str, cache: Path | None = None, retries: int = 3):
        """Parsed JSON from an nseindia.com/api URL ([] when empty)."""
        if cache is None or not cache.exists():
            self._warm()
        data = self.get(url, cache, retries)
        return json.loads(data) if data else []
