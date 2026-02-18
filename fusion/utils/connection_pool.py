"""Connection pooling with retry logic for HTTP requests."""

import logging
import time
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from fusion.config import config

logger = logging.getLogger(__name__)


class ConnectionPool:
    """HTTP connection pool with retry logic and timeout handling.
    
    Uses requests.Session with connection pooling and exponential backoff retry.
    """

    def __init__(
        self,
        pool_size: int = None,
        max_overflow: int = None,
        max_retries: int = None,
        backoff_factor: float = None,
        timeout: int = None,
    ):
        self.pool_size = pool_size or config.POOL_SIZE
        self.max_overflow = max_overflow or config.POOL_MAX_OVERFLOW
        self.max_retries = max_retries or config.WARP_MAX_RETRIES
        self.backoff_factor = backoff_factor or config.WARP_BACKOFF_FACTOR
        self.timeout = timeout or config.WARP_TIMEOUT
        
        # Create session with connection pooling
        self.session = requests.Session()
        
        # Configure retry strategy
        retry_strategy = Retry(
            total=self.max_retries,
            backoff_factor=self.backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS"],
        )
        
        # Mount adapter with retry strategy
        adapter = HTTPAdapter(
            pool_connections=self.pool_size,
            pool_maxsize=self.pool_size + self.max_overflow,
            max_retries=retry_strategy,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        logger.info(
            f"ConnectionPool initialized: pool_size={self.pool_size}, "
            f"max_retries={self.max_retries}, timeout={self.timeout}s"
        )
    
    def request(
        self,
        method: str,
        url: str,
        timeout: Optional[int] = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Make HTTP request with retry logic.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            url: Request URL
            timeout: Request timeout (uses default if not specified)
            **kwargs: Additional arguments passed to requests
        
        Returns:
            Response object
        
        Raises:
            requests.RequestException: On request failure after retries
        """
        request_timeout = timeout or self.timeout
        start_time = time.time()
        
        try:
            response = self.session.request(
                method=method,
                url=url,
                timeout=request_timeout,
                **kwargs,
            )
            
            duration = time.time() - start_time
            logger.debug(
                f"HTTP {method} {url} -> {response.status_code} ({duration:.2f}s)"
            )
            
            response.raise_for_status()
            return response
        
        except requests.RequestException as e:
            duration = time.time() - start_time
            logger.error(
                f"HTTP {method} {url} failed after {duration:.2f}s: {e}"
            )
            raise
    
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """Make GET request."""
        return self.request("GET", url, **kwargs)
    
    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """Make POST request."""
        return self.request("POST", url, **kwargs)
    
    def put(self, url: str, **kwargs: Any) -> requests.Response:
        """Make PUT request."""
        return self.request("PUT", url, **kwargs)
    
    def delete(self, url: str, **kwargs: Any) -> requests.Response:
        """Make DELETE request."""
        return self.request("DELETE", url, **kwargs)
    
    def close(self) -> None:
        """Close session and release connections."""
        self.session.close()
        logger.info("ConnectionPool closed")
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
