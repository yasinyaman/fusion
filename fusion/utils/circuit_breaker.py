"""Circuit breaker pattern implementation for external service calls."""

import logging
import time
from enum import Enum
from typing import Callable, Any, Optional

from fusion.config import config

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, block requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """Circuit breaker to prevent cascading failures.
    
    Tracks failures and opens the circuit after threshold is exceeded.
    After timeout, allows one request through to test recovery.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = None,
        timeout: int = None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold or config.CIRCUIT_BREAKER_THRESHOLD
        self.timeout = timeout or config.CIRCUIT_BREAKER_TIMEOUT
        
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.last_success_time: Optional[float] = None
    
    def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """Execute function through circuit breaker.
        
        Args:
            func: Function to call
            *args, **kwargs: Arguments to pass to function
        
        Returns:
            Result from function call
        
        Raises:
            Exception: If circuit is open or function fails
        """
        if self.state == CircuitState.OPEN:
            if self._should_attempt_reset():
                self.state = CircuitState.HALF_OPEN
                logger.info(f"Circuit breaker '{self.name}' entering HALF_OPEN state")
            else:
                raise Exception(
                    f"Circuit breaker '{self.name}' is OPEN. "
                    f"Service unavailable. Retry after {self.timeout}s"
                )
        
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise e
    
    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset."""
        if self.last_failure_time is None:
            return True
        return time.time() - self.last_failure_time >= self.timeout
    
    def _on_success(self) -> None:
        """Handle successful call."""
        self.failure_count = 0
        self.last_success_time = time.time()
        
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            logger.info(f"Circuit breaker '{self.name}' reset to CLOSED")
    
    def _on_failure(self) -> None:
        """Handle failed call."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.failure_count >= self.failure_threshold:
            if self.state != CircuitState.OPEN:
                self.state = CircuitState.OPEN
                logger.error(
                    f"Circuit breaker '{self.name}' tripped to OPEN after "
                    f"{self.failure_count} failures"
                )
    
    def reset(self) -> None:
        """Manually reset circuit breaker to CLOSED state."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        logger.info(f"Circuit breaker '{self.name}' manually reset")
    
    def get_state(self) -> dict:
        """Get current circuit breaker state."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
            "last_failure_time": self.last_failure_time,
            "last_success_time": self.last_success_time,
        }
