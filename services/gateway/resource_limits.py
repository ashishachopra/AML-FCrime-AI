"""Local resource boundaries. Distributed deployments still need shared edge quotas."""

import asyncio
import json
import math
import os
import time


class PrincipalLimiter:
    def __init__(self, *, per_minute=120, burst=30, max_principals=4096, clock=time.monotonic):
        if min(per_minute, burst, max_principals) <= 0:
            raise ValueError("principal limits must be positive")
        self.rate, self.burst, self.maximum = per_minute / 60, burst, max_principals
        self.clock, self.buckets = clock, {}

    def admit(self, subject: str) -> int:
        """Return zero on admission, otherwise a Retry-After duration in seconds."""
        now = self.clock()
        if subject not in self.buckets and len(self.buckets) >= self.maximum:
            ttl = max(60, self.burst / self.rate)
            self.buckets = {
                key: value for key, value in self.buckets.items() if now - value[1] < ttl
            }
            if len(self.buckets) >= self.maximum:
                return 60  # Never evict a depleted live bucket to admit a new identity.
        tokens, previous = self.buckets.get(subject, (self.burst, now))
        tokens = min(self.burst, tokens + max(now - previous, 0) * self.rate)
        accepted = tokens >= 1
        self.buckets[subject] = (tokens - 1 if accepted else tokens, now)
        return 0 if accepted else max(1, math.ceil((1 - tokens) / self.rate))


class ResourceGuard:
    """Bound actual request bytes before parsing and cap concurrent request work."""

    def __init__(
        self,
        app,
        *,
        max_inflight=None,
        json_bytes=None,
        batch_bytes=None,
        body_timeout=None,
        request_timeout=None,
    ):
        self.app = app
        self.maximum = (
            max_inflight
            if max_inflight is not None
            else int(os.getenv("GATEWAY_MAX_INFLIGHT", "16"))
        )
        self.json_bytes = (
            json_bytes
            if json_bytes is not None
            else int(os.getenv("GATEWAY_MAX_JSON_BYTES", "65536"))
        )
        self.batch_bytes = (
            batch_bytes
            if batch_bytes is not None
            else int(os.getenv("GATEWAY_MAX_BATCH_BYTES", "16777216"))
        )
        self.body_timeout = (
            body_timeout
            if body_timeout is not None
            else float(os.getenv("GATEWAY_BODY_TIMEOUT_SECONDS", "5"))
        )
        self.request_timeout = (
            request_timeout
            if request_timeout is not None
            else float(os.getenv("GATEWAY_REQUEST_TIMEOUT_SECONDS", "35"))
        )
        if min(self.maximum, self.json_bytes, self.batch_bytes) <= 0 or not all(
            math.isfinite(value) and value > 0
            for value in (self.body_timeout, self.request_timeout)
        ):
            raise ValueError("gateway resource bounds must be positive and finite")
        self.active = 0

    @staticmethod
    async def error(send, status, detail):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"retry-after", b"1")],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps({"detail": detail}).encode()})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.active >= self.maximum:
            return await self.error(send, 503, "gateway busy; retry later")
        self.active += 1
        started, ended = False, False

        async def tracked_send(message):
            nonlocal started, ended
            started |= message["type"] == "http.response.start"
            ended |= message["type"] == "http.response.body" and not message.get("more_body", False)
            await send(message)

        try:
            limit = self.batch_bytes if scope["path"] == "/v1/batch" else self.json_bytes
            headers = dict(scope.get("headers", []))
            if headers.get(b"content-encoding", b"identity").lower() != b"identity":
                return await self.error(send, 415, "compressed request bodies are not supported")
            if b"content-length" in headers:
                try:
                    declared = int(headers[b"content-length"])
                    if declared < 0:
                        raise ValueError
                except ValueError:
                    return await self.error(send, 400, "invalid content length")
                if declared > limit:
                    return await self.error(send, 413, "request exceeds byte limit")
            body = bytearray()
            try:
                async with asyncio.timeout(self.body_timeout):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > limit:
                            return await self.error(send, 413, "request exceeds byte limit")
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await self.error(send, 408, "request body deadline exceeded")
            body = bytes(body)
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            try:
                async with asyncio.timeout(self.request_timeout):
                    await self.app(scope, replay, tracked_send)
            except TimeoutError:
                if not started:
                    await self.error(send, 504, "request deadline exceeded; outcome may be unknown")
                elif not ended:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            self.active -= 1
