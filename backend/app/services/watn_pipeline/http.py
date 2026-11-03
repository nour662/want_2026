"""
Thread-safe HTTP helpers.

Many companies are processed at once, so every request goes through a per-host
rate limiter and a retrying session. That keeps parallel workers from tripping
USAspending / NSF / SAM rate limits.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

USER_AGENT = "WATN-Pipeline/1.0 (+https://www.tedcomd.com; research use)"

# Minimum seconds between requests to the same host.
DEFAULT_HOST_INTERVALS = {
    "api.usaspending.gov": 0.75,  # drops connections from clients that go faster
    "api.nsf.gov": 0.25,
    "api.sam.gov": 1.0,
    "api.www.sbir.gov": 0.5,
    "www.tedcomd.com": 0.5,
}


class HostRateLimiter:
    def __init__(self, intervals: Optional[Dict[str, float]] = None, default_interval: float = 0.2):
        self._intervals = dict(DEFAULT_HOST_INTERVALS)
        if intervals:
            self._intervals.update(intervals)
        self._default = default_interval
        self._next_allowed: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        interval = self._intervals.get(host, self._default)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_allowed.get(host, 0.0))
            self._next_allowed[host] = slot + interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


_thread_local = threading.local()
_limiter = HostRateLimiter()


def _session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        retry = Retry(
            total=6,
            connect=6,
            read=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
        )
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers["User-Agent"] = USER_AGENT
        _thread_local.session = session
    return session


def request(method: str, url: str, *, timeout: int = 30, **kwargs: Any) -> requests.Response:
    """Rate-limited, retrying request. Raises on HTTP errors after retries."""
    _limiter.wait(urlparse(url).hostname or "")
    response = _session().request(method, url, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response


def get_json(url: str, **kwargs: Any) -> Any:
    return request("GET", url, headers={"Accept": "application/json"}, **kwargs).json()


def post_json(url: str, payload: Dict[str, Any], **kwargs: Any) -> Any:
    return request("POST", url, json=payload, headers={"Accept": "application/json"}, **kwargs).json()
