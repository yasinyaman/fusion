"""requests.Session with connection pooling and retry/backoff."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class ConnectionPool:
    """Pooled HTTP session with exponential-backoff retries on transient errors.

    Redirects are never followed (SSRF guard) and the Warp API key, when
    given, is sent as a bearer token on every request.
    """

    def __init__(
        self,
        pool_size: int = 10,
        max_overflow: int = 5,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
        timeout: float = 30.0,
        api_key: str | None = None,
    ) -> None:
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout

        self.session = requests.Session()
        self.session.max_redirects = 0
        self.session.headers["Accept"] = "application/json"
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"

        retry = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS"],
            # Return the last response instead of raising MaxRetryError so the
            # caller sees the real status code via raise_for_status().
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            pool_connections=pool_size,
            pool_maxsize=pool_size + max_overflow,
            max_retries=retry,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        logger.info(
            "ConnectionPool initialized: pool_size=%d, max_retries=%d, timeout=%ss",
            pool_size,
            max_retries,
            timeout,
        )

    def request(
        self, method: str, url: str, timeout: float | None = None, **kwargs: Any
    ) -> requests.Response:
        """Perform a request and raise ``requests.RequestException`` on failure."""
        started = time.time()
        try:
            response = self.session.request(
                method=method,
                url=url,
                timeout=timeout or self.timeout,
                allow_redirects=False,
                **kwargs,
            )
            logger.debug(
                "HTTP %s %s -> %s (%.2fs)",
                method,
                url,
                response.status_code,
                time.time() - started,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            logger.error("HTTP %s %s failed after %.2fs: %s", method, url, time.time() - started, e)
            raise

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self.session.close()
        logger.info("ConnectionPool closed")

    def __enter__(self) -> ConnectionPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
