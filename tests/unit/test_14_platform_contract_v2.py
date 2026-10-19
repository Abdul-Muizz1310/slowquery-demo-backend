"""Spec 14: the platform token contract v2, against the composed app.

Every case drives ``create_app()`` (real middleware order, real routers, real
dependencies) with throwaway Ed25519 keys made per run; only the database
session is a stand-in. Conformance cases 1-14 are numbered as in the
workspace contract, slowquery-specific cases continue from 15.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

PROTECTED = "/branches/current"
_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class _Pair:
    private_pem: str
    public_b64: str


def _pair() -> _Pair:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return _Pair(private_pem=pem, public_b64=base64.b64encode(der).decode())


BASTION = _pair()
FRONTEND = _pair()
STRANGER = _pair()


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "bastion",
        "aud": "slowquery",
        "service": "slowquery",
        "sub": "v_abcdefghij",
        "role": "viewer",
        "cap": "slowquery.view",
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
    }
    for key, value in overrides.items():
        if value is _DROP:
            claims.pop(key, None)
        else:
            claims[key] = value
    return claims


_DROP = object()


def _mint(pair: _Pair = BASTION, **overrides: Any) -> str:
    return jwt.encode(
        _claims(**overrides), pair.private_pem, algorithm="EdDSA", headers={"kid": "test-key"}
    )


def _frontend_token(**overrides: Any) -> str:
    base = {"iss": "slowquery-frontend", "sub": "slowquery-frontend", "role": "frontend"}
    base.update(overrides)
    return _mint(FRONTEND, **base)


@pytest.fixture(autouse=True)
def _contract_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Production-like settings: enforcement on, both keys configured."""
    from slowquery_demo.core import platform_keys

    for name in (
        "BASTION_SIGNING_KEY_PUBLIC",
        "BASTION_PUBLIC_KEY_URL",
        "SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC",
        "BASTION_EXPECTED_ISSUER",
        "BASTION_EXPECTED_AUDIENCE",
        "BASTION_EXPECTED_SERVICE",
        "BASTION_ALLOWED_ROLES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", BASTION.public_b64)
    monkeypatch.setenv("SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC", FRONTEND.public_b64)
    platform_keys.reset_key_cache()
    yield
    platform_keys.reset_key_cache()


def _client(**env: str) -> TestClient:
    """A fresh app (no lifespan: no drainer, no DB) with a stand-in session."""
    import os

    from slowquery_demo.core.database import get_db
    from slowquery_demo.main import create_app

    for key, value in env.items():
        if value == "":
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    from tests.test_health import _OkEngine

    app = create_app()
    app.state.branch_switcher._engine_builder = None
    app.state.engine = _OkEngine()  # /health answers 200 without a database

    async def _session() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()
        result = AsyncMock()
        result.scalar_one_or_none = lambda: None
        result.scalars = lambda: _Empty()
        result.all = lambda: []
        session.execute.return_value = result
        yield session

    app.dependency_overrides[get_db] = _session
    return TestClient(app, raise_server_exceptions=False)


class _Empty:
    def all(self) -> list[object]:
        return []


def _get(
    client: TestClient, token: str | None = None, path: str = PROTECTED, **headers: str
) -> Any:
    if token is not None:
        headers["X-Platform-Token"] = token
    return client.get(path, headers=headers)


def _detail(resp: Any) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("detail") if isinstance(body, dict) else None


def _assert_invalid(resp: Any) -> None:
    assert resp.status_code == 401, resp.text
    assert _detail(resp) == "invalid_platform_token"


# --- conformance (contract §3) ---------------------------------------------


def test_01_valid_bastion_token_is_authenticated() -> None:
    resp = _get(_client(), _mint())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"active": "slow"}


@pytest.mark.parametrize("aud", ["paper-trail", "slowquery-demo", "inkprint", "SLOWQUERY"])
def test_02_token_for_another_service_is_invalid(aud: str) -> None:
    _assert_invalid(_get(_client(), _mint(aud=aud)))


@pytest.mark.parametrize("aud", [_DROP, ["slowquery"], ["slowquery", "magpie"], ""])
def test_03_missing_or_list_aud_is_invalid(aud: object) -> None:
    _assert_invalid(_get(_client(), _mint(aud=aud)))


def test_04_the_key_decides_the_issuer() -> None:
    client = _client()
    _assert_invalid(_get(client, _mint(FRONTEND, iss="bastion")))
    _assert_invalid(_get(client, _mint(BASTION, iss="slowquery-frontend", role="frontend")))


@pytest.mark.parametrize("role", ["viewer", "admin", _DROP, ""])
def test_05_frontend_key_requires_role_frontend(role: object) -> None:
    _assert_invalid(_get(_client(), _frontend_token(role=role)))


def test_06_exp_has_ten_seconds_of_leeway() -> None:
    now = int(time.time())
    client = _client()
    assert _get(client, _mint(iat=now - 65, exp=now - 5)).status_code == 200
    _assert_invalid(_get(client, _mint(iat=now - 75, exp=now - 15)))


def test_07_iat_has_ten_seconds_of_leeway() -> None:
    now = int(time.time())
    client = _client()
    assert _get(client, _mint(iat=now + 5, exp=now + 65)).status_code == 200
    _assert_invalid(_get(client, _mint(iat=now + 15, exp=now + 75)))


def _unsigned(header: dict[str, Any], claims: dict[str, Any], signature: bytes = b"") -> str:
    def part(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return ".".join(
        [part(json.dumps(header).encode()), part(json.dumps(claims).encode()), part(signature)]
    )


def test_08_alg_none_and_hs256_are_invalid() -> None:
    client = _client()
    _assert_invalid(_get(client, _unsigned({"alg": "none", "typ": "JWT"}, _claims())))
    hs256 = jwt.encode(_claims(), "shared-secret-of-at-least-32-bytes!!", algorithm="HS256")
    _assert_invalid(_get(client, hs256))
    # The public key's own text as an HMAC secret: the classic alg-confusion attack.
    confused = jwt.encode(_claims(), BASTION.public_b64, algorithm="HS256")
    _assert_invalid(_get(client, confused))


def test_09_tampered_payload_is_invalid() -> None:
    header, _, signature = _mint().split(".")
    forged = base64.urlsafe_b64encode(json.dumps(_claims(sub="v_attacker00")).encode())
    tampered = ".".join([header, forged.rstrip(b"=").decode(), signature])
    client = _client()
    _assert_invalid(_get(client, tampered))
    _assert_invalid(_get(client, _mint(STRANGER)))
    _assert_invalid(_get(client, "not-a-jwt"))


def test_10_missing_token_depends_on_demo_mode(caplog: pytest.LogCaptureFixture) -> None:
    resp = _get(_client())
    assert resp.status_code == 401
    assert _detail(resp) == "missing_platform_token"

    with caplog.at_level(logging.WARNING, logger="slowquery_demo.core.platform_auth"):
        resp = _get(_client(DEMO_MODE="true"))
    assert resp.status_code == 200
    assert any("DEMO_MODE" in r.getMessage() for r in caplog.records)


def test_11_invalid_token_is_rejected_even_in_demo_mode() -> None:
    _assert_invalid(_get(_client(DEMO_MODE="true"), _mint(aud="paper-trail")))


@pytest.mark.parametrize("demo", ["false", "true"])
def test_12_no_trusted_key_fails_closed(demo: str) -> None:
    client = _client(
        BASTION_SIGNING_KEY_PUBLIC="", SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC="", DEMO_MODE=demo
    )
    resp = _get(client, _mint())
    assert resp.status_code == 503
    assert _detail(resp) == "platform_auth_unconfigured"
    assert _get(client).status_code == 503

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["db"] == "ok"


def test_12_test_settings_skip_the_fail_closed_answer() -> None:
    client = _client(
        APP_ENV="test",
        BASTION_SIGNING_KEY_PUBLIC="",
        SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC="",
        DEMO_MODE="true",
    )
    assert _get(client).status_code == 200
    _assert_invalid(_get(client, _mint()))


_UID = "3f1d8e2a-0000-4000-8000-000000000001"
PUBLIC_ROUTES = [
    "/health",
    "/version",
    "/metrics",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/",
    "/users",
    "/users?limit=5",
    f"/users/{_UID}",
    f"/users/{_UID}/orders",
    "/products",
    f"/products/{_UID}",
    "/orders",
    f"/orders/{_UID}",
    f"/order_items?product_id={_UID}",
]


@pytest.mark.parametrize("path", PUBLIC_ROUTES)
def test_13_public_routes_answer_without_a_token(path: str) -> None:
    resp = _client().get(path, follow_redirects=False)
    assert resp.status_code not in (401, 503), (path, resp.status_code, resp.text)
    assert _detail(resp) not in {
        "missing_platform_token",
        "invalid_platform_token",
        "platform_auth_unconfigured",
    }


def test_13_public_routes_stay_public_with_no_key_configured() -> None:
    client = _client(BASTION_SIGNING_KEY_PUBLIC="", SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC="")
    for path in PUBLIC_ROUTES:
        assert client.get(path, follow_redirects=False).status_code not in (401, 503), path


def test_14_rate_limit_key_bastion_sub_and_frontend_address() -> None:
    """Contract §2 as amended 2026-10-20: bastion:{sub}; frontend {iss}:{forwarded address}."""
    client = _client(DEMO_MUTATION_COOLDOWN_S="60")

    def switch(token: str, ip: str | None, target: str) -> int:
        headers = {"X-Platform-Token": token}
        if ip is not None:
            headers["X-Forwarded-For"] = ip
        return client.post("/branches/switch", json={"target": target}, headers=headers).status_code

    # bastion: the bucket follows the visitor pseudonym, not the address.
    assert switch(_mint(sub="v_alice00000"), "203.0.113.1", "fast") == 200
    assert switch(_mint(sub="v_alice00000"), "198.51.100.9", "slow") == 429
    assert switch(_mint(sub="v_bob0000000"), "203.0.113.1", "slow") == 200

    # frontend: sub is constant, so two visitors through the dashboard get two buckets...
    assert switch(_frontend_token(), "192.0.2.10", "fast") == 200
    assert switch(_frontend_token(), "192.0.2.11", "slow") == 200
    # ...and one visitor keeps its own bucket.
    assert switch(_frontend_token(), "192.0.2.10", "slow") == 429
    # A frontend visitor and a bastion visitor never share a bucket.
    assert switch(_mint(sub="slowquery-frontend"), "192.0.2.10", "fast") == 200


# --- slowquery-specific ------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/_slowquery/queries"),
        ("GET", "/_slowquery/queries/7f9c44d378fe11e2"),
        ("GET", "/_slowquery/api/stream"),
        ("POST", "/_slowquery/queries/7f9c44d378fe11e2/force-explain"),
        ("POST", "/branches/switch"),
        ("GET", "/branches/current"),
        ("POST", "/users"),
        ("DELETE", f"/users/{_UID}"),
        ("PUT", f"/orders/{_UID}"),
        ("POST", "/order_items"),
        ("GET", "/branches"),
        ("GET", "/_slowquery"),
    ],
)
def test_15_protected_routes_need_a_token(method: str, path: str) -> None:
    resp = _client().request(method, path, json={"target": "fast"})
    assert resp.status_code == 401, (method, path, resp.status_code)
    assert _detail(resp) == "missing_platform_token"


def test_16_frontend_token_is_authenticated() -> None:
    resp = _get(_client(), _frontend_token())
    assert resp.status_code == 200, resp.text


def test_17_bearer_fallback_and_header_precedence() -> None:
    client = _client()
    assert client.get(PROTECTED, headers={"Authorization": f"Bearer {_mint()}"}).status_code == 200
    _assert_invalid(
        client.get(
            PROTECTED,
            headers={
                "X-Platform-Token": _mint(aud="magpie"),
                "Authorization": f"Bearer {_mint()}",
            },
        )
    )
    resp = client.get(PROTECTED, headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert _detail(resp) == "missing_platform_token"


@pytest.mark.parametrize(
    "overrides",
    [
        {"iat": 1_000, "exp": 1_000 + 301},
        {"exp": float(int(time.time()) + 60)},
        {"iat": str(int(time.time()))},
        {"exp": True},
        {"exp": _DROP},
        {"iat": _DROP},
        {"sub": _DROP},
        {"sub": ""},
        {"sub": 42},
    ],
)
def test_18_lifetime_types_and_sub(overrides: dict[str, Any]) -> None:
    if overrides.get("iat") == 1_000:
        now = int(time.time())
        overrides = {"iat": now, "exp": now + 301}
    _assert_invalid(_get(_client(), _mint(**overrides)))


def test_18_lifetime_of_exactly_300_seconds_is_allowed() -> None:
    now = int(time.time())
    assert _get(_client(), _mint(iat=now, exp=now + 300)).status_code == 200


def test_19_rejection_log_names_the_check_not_the_token(caplog: pytest.LogCaptureFixture) -> None:
    token = _mint(aud="paper-trail")
    with caplog.at_level(logging.WARNING, logger="slowquery_demo.core.platform_auth"):
        _assert_invalid(_get(_client(), token))
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "aud" in messages
    assert token not in messages
    assert token.split(".")[1] not in messages


@pytest.mark.parametrize(
    "bad",
    ["not base64 !!", base64.b64encode(b"junk-bytes").decode(), "﻿  "],
)
def test_20_malformed_key_is_ignored_with_a_named_warning(
    bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="slowquery_demo.core.platform_keys"):
        client = _client(SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC=bad, BASTION_SIGNING_KEY_PUBLIC="")
        resp = _get(client, _frontend_token())
    assert resp.status_code == 503
    if bad.strip("﻿ "):
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC" in messages
        assert bad not in messages


def test_20_rsa_key_is_not_an_ed25519_key() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_der = (
        rsa.generate_private_key(public_exponent=65537, key_size=1024)
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    client = _client(BASTION_SIGNING_KEY_PUBLIC=base64.b64encode(rsa_der).decode())
    # bastion's key is unusable, the frontend key still works.
    _assert_invalid(_get(client, _mint()))
    assert _get(client, _frontend_token()).status_code == 200


class _KeyEndpoint:
    """Stands in for ``httpx.AsyncClient`` serving bastion's public-key JSON."""

    calls = 0
    fail = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _KeyEndpoint:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str) -> Any:
        type(self).calls += 1
        if type(self).fail:
            raise ConnectionError("unreachable")

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, str]:
                return {"kid": "bastion-ed25519-2026-04", "publicKey": BASTION.public_b64}

        return _Resp()


def test_21_bastion_key_from_url_is_fetched_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from slowquery_demo.core import platform_keys

    _KeyEndpoint.calls, _KeyEndpoint.fail = 0, False
    monkeypatch.setattr(platform_keys.httpx, "AsyncClient", _KeyEndpoint)
    client = _client(
        BASTION_SIGNING_KEY_PUBLIC="", BASTION_PUBLIC_KEY_URL="https://bastion.test/api/key"
    )
    assert _get(client, _mint()).status_code == 200
    assert _get(client, _mint()).status_code == 200
    assert _KeyEndpoint.calls == 1


def test_21_failed_fetch_leaves_bastion_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    from slowquery_demo.core import platform_keys

    _KeyEndpoint.calls, _KeyEndpoint.fail = 0, True
    monkeypatch.setattr(platform_keys.httpx, "AsyncClient", _KeyEndpoint)
    client = _client(
        BASTION_SIGNING_KEY_PUBLIC="",
        SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC="",
        BASTION_PUBLIC_KEY_URL="https://bastion.test/api/key",
    )
    assert _get(client, _mint()).status_code == 503


def test_22_anonymous_bucket_is_the_leftmost_forwarded_address() -> None:
    client = _client(DEMO_MODE="true", DEMO_MUTATION_COOLDOWN_S="60")

    def switch(xff: str | None, target: str) -> int:
        headers = {"X-Forwarded-For": xff} if xff else {}
        return client.post("/branches/switch", json={"target": target}, headers=headers).status_code

    assert switch("203.0.113.1, 10.0.0.1", "fast") == 200
    assert switch("203.0.113.1, 10.9.9.9", "slow") == 429
    assert switch("198.51.100.2", "slow") == 200
    assert switch(None, "fast") == 200  # the socket peer, "testclient"
    assert switch(None, "slow") == 429


def test_22_rate_limit_key_function() -> None:
    from starlette.requests import Request

    from slowquery_demo.core.access import rate_limit_key
    from slowquery_demo.core.platform_token import Principal

    def request(headers: dict[str, str], principal: Principal | None = None) -> Request:
        scope: dict[str, Any] = {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("192.0.2.7", 1234),
            "state": {},
        }
        req = Request(scope)
        if principal is not None:
            req.state.platform_principal = principal
        return req

    assert rate_limit_key(request({})) == "192.0.2.7"
    assert rate_limit_key(request({"X-Forwarded-For": " 203.0.113.5 , 10.0.0.1"})) == "203.0.113.5"
    assert rate_limit_key(request({"X-Forwarded-For": " , "})) == "192.0.2.7"
    bastion = Principal(iss="bastion", sub="v_abcdefghij", role="viewer")
    assert rate_limit_key(request({"X-Forwarded-For": "1.2.3.4"}, bastion)) == (
        "bastion:v_abcdefghij"
    )
    frontend = Principal(iss="slowquery-frontend", sub="slowquery-frontend", role="frontend")
    assert rate_limit_key(request({"X-Forwarded-For": "1.2.3.4, 10.0.0.1"}, frontend)) == (
        "slowquery-frontend:1.2.3.4"
    )
    assert rate_limit_key(request({}, frontend)) == "slowquery-frontend:192.0.2.7"


def test_23_cors_preflight_and_401_carry_cors_headers() -> None:
    client = _client(CORS_ORIGINS="https://slowquery-dashboard-frontend.vercel.app")
    origin = "https://slowquery-dashboard-frontend.vercel.app"
    pre = client.options(
        PROTECTED,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-platform-token, x-request-id, content-type",
        },
    )
    assert pre.status_code == 200
    allowed = {h.strip() for h in pre.headers["access-control-allow-headers"].lower().split(",")}
    assert {"x-platform-token", "x-request-id", "content-type"} <= allowed

    rejected = client.get(PROTECTED, headers={"Origin": origin})
    assert rejected.status_code == 401
    assert rejected.headers["access-control-allow-origin"] == origin
    assert "x-request-id" in rejected.headers


def test_24_demo_mode_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    from slowquery_demo.core.config import Settings

    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert Settings(_env_file=None).demo_mode is False  # type: ignore[call-arg]


def test_25_old_expected_claim_knobs_are_ignored() -> None:
    client = _client(
        BASTION_EXPECTED_AUDIENCE="paper-trail",
        BASTION_EXPECTED_SERVICE="paper-trail",
        BASTION_EXPECTED_ISSUER="someone-else",
        BASTION_ALLOWED_ROLES="admin",
    )
    try:
        assert _get(client, _mint(role="viewer")).status_code == 200
    finally:
        import os

        for name in (
            "BASTION_EXPECTED_AUDIENCE",
            "BASTION_EXPECTED_SERVICE",
            "BASTION_EXPECTED_ISSUER",
            "BASTION_ALLOWED_ROLES",
        ):
            os.environ.pop(name, None)


def test_26_authenticated_stream_is_event_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    import slowquery_demo.api.routers.dashboard as dash

    async def _one_frame(_request: object) -> AsyncIterator[str]:
        yield 'data: {"kind": "heartbeat", "now": "2026-10-20T00:00:00Z"}\n\n'

    monkeypatch.setattr(dash, "_sse_generator", _one_frame)
    client = _client()
    assert _get(client, path="/_slowquery/api/stream").status_code == 401
    resp = _get(client, _frontend_token(), path="/_slowquery/api/stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "heartbeat" in resp.text


def test_27_uvicorn_trusts_the_render_proxy() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text()
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    argv = json.loads(cmd[len("CMD ") :])
    assert "--proxy-headers" in argv
    assert "--forwarded-allow-ips=*" in argv
