import asyncio
import json
import time

import httpx
import pytest
from conftest import load_source_module
from jose import jwt

gateway = load_source_module("guarded_gateway_test", "services/gateway/main.py")


@pytest.fixture
def auth(monkeypatch):
    # Synthetic test signing key only; never read local credentials.
    key = "synthetic-test-signing-key-not-for-production"
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.delenv("JWT_SECRET_KEY_FILE", raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", key)
    monkeypatch.setenv("JWT_ISSUER", "test-issuer")
    monkeypatch.setenv("JWT_AUDIENCE", "test-api")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setattr(gateway, "principal_limiter", gateway.PrincipalLimiter())

    def headers(*, omit=(), **changes):
        now = int(time.time())
        claims = dict(
            sub="test-agent",
            roles=["admin"],
            iss="test-issuer",
            aud="test-api",
            iat=now,
            exp=now + 300,
            principal_type="agent",
        )
        claims.update(changes)
        for field in omit:
            claims.pop(field, None)
        return {"Authorization": "Bearer " + jwt.encode(claims, key, algorithm="HS256")}

    return headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,omitted",
    [
        ({}, ("aud",)),
        ({}, ("iss",)),
        ({"aud": "another-api"}, ()),
        ({"iss": "untrusted"}, ()),
        ({"roles": {"admin": True}}, ()),
        ({"roles": ["admin", 1]}, ()),
        ({"roles": None}, ()),
        ({"principal_type": ["human"]}, ()),
        ({"amr": "mfa"}, ()),
        ({"auth_time": True}, ()),
        ({"auth_time": float("nan")}, ()),
        ({"exp": None}, ()),
        ({"exp": float("inf")}, ()),
        ({"iat": []}, ()),
        ({"nbf": {}}, ()),
        ({}, ("iat",)),
        ({"iat": int(time.time()) - 3600}, ()),
        ({"iat": int(time.time()) + 120}, ()),
        ({"sub": ""}, ()),
        ({"exp": int(time.time()) - 20}, ()),
    ],
)
async def test_invalid_claims_are_401_never_upstream_work(auth, changes, omitted, monkeypatch):
    async def no_upstream(request):
        pytest.fail("invalid identity reached upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_upstream)) as upstream:
        monkeypatch.setattr(gateway, "http_client", upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway"
        ) as client:
            response = await client.get("/v1/alerts", headers=auth(omit=omitted, **changes))
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        {"principal_type": "agent", "amr": ["mfa"], "auth_time": int(time.time())},
        {"principal_type": "service", "amr": ["mfa"], "auth_time": int(time.time())},
        {"omit": ("principal_type",), "amr": ["mfa"], "auth_time": int(time.time())},
        {"principal_type": "human", "amr": ["pwd"], "auth_time": int(time.time())},
        {"principal_type": "human", "amr": ["mfa"], "auth_time": int(time.time()) - 600},
    ],
)
@pytest.mark.parametrize(
    "action",
    [
        {"sar_review_status": "approved"},
        {"sar_review_status": "rejected"},
        {"status": "closed"},
        {"status": "false_positive"},
        {"status": "open"},
        {"status": "investigating"},
    ],
)
async def test_final_actions_require_fresh_human_mfa_even_for_admin(auth, identity, action):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway"
    ) as client:
        response = await client.patch(
            "/v1/alerts/A1", json=dict(expected_revision=1, **action), headers=auth(**identity)
        )
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_human_review_and_agent_assistance_forward_verified_actor_and_revision(
    auth, monkeypatch
):
    received = []

    async def upstream(request):
        received.append(json.loads(request.content))
        # A stale result remains a conflict at the external API boundary.
        return httpx.Response(409, json={"detail": "alert changed; reload evidence"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as inner:
        monkeypatch.setattr(gateway, "http_client", inner)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway"
        ) as client:
            response = await client.patch(
                "/v1/alerts/A1",
                headers=auth(
                    sub="investigator",
                    principal_type="human",
                    amr=["mfa"],
                    auth_time=int(time.time()),
                ),
                json={"expected_revision": 7, "sar_review_status": "approved"},
            )
            assert response.status_code == 409
            response = await client.patch(
                "/v1/alerts/A1",
                headers=auth(),
                json={
                    "expected_revision": 7,
                    "assigned_to": "investigator",
                    "investigation_notes": "Draft suggestion",
                },
            )
            assert response.status_code == 409
            for invalid in (
                {"status": "investigating"},
                {"expected_revision": True},
                {"expected_revision": 7, "actor": "fake-human"},
                {"expected_revision": 7, "principal_type": "human"},
            ):
                response = await client.patch("/v1/alerts/A1", headers=auth(), json=invalid)
                assert response.status_code == 422
    assert len(received) == 2
    assert received[0] == {
        "expected_revision": 7,
        "sar_review_status": "approved",
        "actor": "investigator",
    }
    assert received[1]["actor"] == "test-agent"


@pytest.mark.asyncio
async def test_principal_quota_rejects_before_upstream_and_survives_token_rotation(
    auth, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        gateway, "principal_limiter", gateway.PrincipalLimiter(burst=1, per_minute=1)
    )

    async def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={"alerts": [], "total": 0, "limit": 100, "offset": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as inner:
        monkeypatch.setattr(gateway, "http_client", inner)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway"
        ) as client:
            assert (await client.get("/v1/alerts", headers=auth(jti="first"))).status_code == 200
            exhausted = await client.get("/v1/alerts", headers=auth(jti="rotated"))
            assert exhausted.status_code == 429
            assert int(exhausted.headers["Retry-After"]) > 0
    assert len(calls) == 1


def test_principal_bucket_memory_is_bounded_and_refills_without_identity_eviction():
    now = [0]
    limiter = gateway.PrincipalLimiter(
        burst=1, per_minute=1, max_principals=2, clock=lambda: now[0]
    )
    assert limiter.admit("A") == limiter.admit("B") == 0
    assert limiter.admit("C") == 60
    assert limiter.admit("A") == 60
    now[0] = 30
    assert limiter.admit("A") == 30
    assert len(limiter.buckets) == 2
    now[0] = 61
    assert limiter.admit("C") == 0  # The idle B bucket can now expire.
    assert limiter.admit("A") == 0
    assert len(limiter.buckets) == 2


async def invoke(guard, chunks=(), *, headers=(), path="/v1/evaluate", receive=None):
    messages = iter(chunks)
    sent = []

    async def next_chunk():
        return next(messages, {"type": "http.request", "body": b""})

    async def send(value):
        sent.append(value)

    await guard({"type": "http", "path": path, "headers": headers}, receive or next_chunk, send)
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [(), ((b"content-length", b"1"),)])
async def test_streamed_byte_limit_checks_actual_bytes_before_parser(headers):
    async def not_called(*args):
        pytest.fail("oversized body reached parser")

    guard = gateway.ResourceGuard(not_called, json_bytes=8)
    messages = await invoke(
        guard,
        [
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"6789", "more_body": False},
        ],
        headers=headers,
    )
    assert messages[0]["status"] == 413
    assert guard.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,status",
    [
        ([(b"content-length", b"-1")], 400),
        ([(b"content-length", b"wrong")], 400),
        ([(b"content-length", b"100")], 413),
        ([(b"content-encoding", b"gzip")], 415),
    ],
)
async def test_body_headers_are_checked_before_reading(headers, status):
    async def not_called(*args):
        pytest.fail("invalid headers reached parser or receive")

    guard = gateway.ResourceGuard(not_called, json_bytes=8)
    assert (await invoke(guard, headers=headers, receive=not_called))[0]["status"] == status


@pytest.mark.asyncio
async def test_body_and_processing_deadlines_release_slots():
    async def slow(*args):
        await asyncio.Future()

    guard = gateway.ResourceGuard(slow, body_timeout=0.01, request_timeout=0.01)
    assert (await invoke(guard, receive=slow))[0]["status"] == 408
    assert guard.active == 0
    assert (await invoke(guard))[0]["status"] == 504
    assert guard.active == 0


@pytest.mark.asyncio
async def test_global_concurrency_rejects_without_queue_and_recovers_after_cancellation():
    entered = asyncio.Event()

    async def busy(*args):
        entered.set()
        await asyncio.Future()

    guard = gateway.ResourceGuard(busy, max_inflight=1)
    first = asyncio.create_task(invoke(guard))
    await asyncio.wait_for(entered.wait(), 1)
    assert (await invoke(guard))[0]["status"] == 503
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert guard.active == 0


@pytest.mark.asyncio
async def test_accepted_body_is_replayed_exactly_and_batch_has_separate_cap():
    bodies = []

    async def app(scope, receive, send):
        bodies.append((await receive())["body"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    guard = gateway.ResourceGuard(app, json_bytes=3, batch_bytes=6)
    chunks = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cdef", "more_body": False},
    ]
    assert (await invoke(guard, chunks, path="/v1/batch"))[0]["status"] == 204
    assert bodies == [b"abcdef"]
    assert (await invoke(guard, chunks))[0]["status"] == 413
    assert guard.active == 0
