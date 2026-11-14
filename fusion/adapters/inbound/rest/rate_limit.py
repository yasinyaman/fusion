"""Rate limiting keyed per API key (falls back to client IP)."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from typing import Any

from limits import parse_many
from slowapi import Limiter
from slowapi.util import get_remote_address

from fusion.application.settings import Settings


def rate_limit_key(request: Any, api_key: str = "") -> str:
    """Bucket on a hash of ``X-API-Key`` when it is the real key, else the remote address.

    Keying on the API key resists ``X-Forwarded-For`` spoofing and avoids
    throttling many clients that share one NAT/proxy IP. The key is hashed
    so the raw secret is never used as an in-memory bucket name. A header
    that is *not* the configured key buys nothing: otherwise a caller gets a
    fresh bucket per made-up value.
    """
    provided = request.headers.get("X-API-Key")
    if provided and api_key and hmac.compare_digest(provided.encode(), api_key.encode()):
        return "key:" + hashlib.sha256(provided.encode()).hexdigest()[:16]
    return str(get_remote_address(request))


def make_limiter(settings: Settings) -> Limiter:
    return Limiter(
        key_func=lambda request: rate_limit_key(request, settings.api_key),
        default_limits=[settings.rate_limit],
    )


def failed_key_counter(limiter: Limiter, settings: Settings) -> Callable[[Any], bool]:
    """Count a rejected ``X-API-Key`` against its client; True once over the limit.

    The limiter middleware skips requests it has no route limit for (unknown
    paths, wrong methods, routes with their own decorator), so wrong keys
    there would be free. Auth calls this on every rejection instead.
    """
    items = parse_many(settings.rate_limit)

    def over_limit(request: Any) -> bool:
        address = str(get_remote_address(request))
        allowed = [limiter.limiter.hit(item, "auth-failure", address) for item in items]
        return not all(allowed)

    return over_limit
