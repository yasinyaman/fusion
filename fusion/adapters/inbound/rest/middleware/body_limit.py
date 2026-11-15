"""Refuse request bodies over a size limit before anything parses them."""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyLimitMiddleware:
    """413 for a body over ``max_bytes``, declared or streamed.

    A plain ASGI middleware rather than ``BaseHTTPMiddleware``: it has to see
    the body as it arrives, so a chunked upload with no ``Content-Length`` is
    cut off at the limit too instead of being read whole first.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self._app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._max <= 0:
            await self._app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max:
            await self._refuse(send)
            return

        received = 0
        too_big = False

        async def limited() -> Message:
            nonlocal received, too_big
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max:
                    too_big = True
                    # End the body here; the app sees a truncated request and
                    # the response below replaces whatever it would answer.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if too_big:
                if not started:
                    started = True
                    await self._refuse(send)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self._app(scope, limited, guarded_send)

    async def _refuse(self, send: Send) -> None:
        body = json.dumps(
            {"error": f"Request body is over {self._max:,} bytes (FUSION_MAX_REQUEST_BYTES)."}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
