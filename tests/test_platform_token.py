"""Tests for the X-Platform-Token middleware (Ed25519 platform JWT verification)."""

from __future__ import annotations

import base64
import datetime as dt

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slowquery_demo.core import platform_token
from slowquery_demo.core.platform_token import install_platform_token


def _make_app(*, demo_mode: bool) -> TestClient:
    app = FastAPI()
    install_platform_token(app, demo_mode=demo_mode)

    @app.get("/health", include_in_schema=False)
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/protected")
    async def _protected() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def _keypair() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_der = priv.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, base64.b64encode(pub_der).decode()


def _token(priv_pem: str, *, expired: bool = False) -> str:
    now = dt.datetime.now(dt.UTC)
    exp = now - dt.timedelta(seconds=10) if expired else now + dt.timedelta(seconds=60)
    return jwt.encode(
        {"sub": "bastion", "role": "admin", "service": "slowquery-demo", "exp": exp},
        priv_pem,
        algorithm="EdDSA",
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BASTION_SIGNING_KEY_PUBLIC", raising=False)
    monkeypatch.delenv("BASTION_PUBLIC_KEY_URL", raising=False)
    monkeypatch.delenv("BASTION_EXPECTED_SERVICE", raising=False)
    monkeypatch.delenv("BASTION_EXPECTED_ISSUER", raising=False)
    monkeypatch.delenv("BASTION_EXPECTED_AUDIENCE", raising=False)
    monkeypatch.delenv("BASTION_ALLOWED_ROLES", raising=False)
    platform_token.reset_public_key_cache()


def test_demo_mode_accepts_without_token() -> None:
    client = _make_app(demo_mode=True)
    assert client.get("/protected").status_code == 200


def test_non_demo_without_key_fails_open() -> None:
    client = _make_app(demo_mode=False)
    assert client.get("/protected").status_code == 200


def test_non_demo_with_key_rejects_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", pub_b64)
    client = _make_app(demo_mode=False)
    assert client.get("/protected").status_code == 401
    # Platform endpoints stay exempt even when enforcement is on.
    assert client.get("/health").status_code == 200


def test_non_demo_with_key_accepts_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    priv_pem, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", pub_b64)
    client = _make_app(demo_mode=False)
    headers = {"X-Platform-Token": _token(priv_pem)}
    assert client.get("/protected", headers=headers).status_code == 200


def test_non_demo_with_key_rejects_tampered_token(monkeypatch: pytest.MonkeyPatch) -> None:
    priv_pem, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", pub_b64)
    client = _make_app(demo_mode=False)
    headers = {"X-Platform-Token": f"{_token(priv_pem)}tampered"}
    assert client.get("/protected", headers=headers).status_code == 401


def test_non_demo_with_key_rejects_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    priv_pem, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", pub_b64)
    client = _make_app(demo_mode=False)
    headers = {"X-Platform-Token": _token(priv_pem, expired=True)}
    assert client.get("/protected", headers=headers).status_code == 401


def test_public_key_fetched_from_url_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    priv_pem, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_PUBLIC_KEY_URL", "https://bastion.example/api/public-key")

    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"kid": "test", "algorithm": "EdDSA", "publicKey": pub_b64}

    class _FakeAsyncClient:
        """Stand-in for ``httpx.AsyncClient`` used as an async context manager."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str) -> _Resp:
            calls["n"] += 1
            return _Resp()

    monkeypatch.setattr(platform_token.httpx, "AsyncClient", _FakeAsyncClient)
    client = _make_app(demo_mode=False)
    headers = {"X-Platform-Token": _token(priv_pem)}
    assert client.get("/protected", headers=headers).status_code == 200
    # Second request reuses the cached key (no second fetch).
    assert client.get("/protected", headers=headers).status_code == 200
    assert calls["n"] == 1


def test_load_public_key_pem_is_async() -> None:
    """P10 regression: the key loader must be a coroutine (no blocking get)."""
    import inspect

    assert inspect.iscoroutinefunction(platform_token.load_public_key_pem)


def test_rejects_token_for_different_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-1: a valid token minted for a sibling service is rejected."""
    priv_pem, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", pub_b64)
    monkeypatch.setenv("BASTION_EXPECTED_SERVICE", "slowquery-demo")
    client = _make_app(demo_mode=False)
    # Token's service claim is "slowquery-demo" (see _token) → accepted.
    assert (
        client.get("/protected", headers={"X-Platform-Token": _token(priv_pem)}).status_code == 200
    )

    # A token for another service must be rejected even though the signature is valid.
    import datetime as dt

    other = jwt.encode(
        {
            "sub": "bastion",
            "role": "admin",
            "service": "paper-trail",
            "exp": dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60),
        },
        priv_pem,
        algorithm="EdDSA",
    )
    assert client.get("/protected", headers={"X-Platform-Token": other}).status_code == 401


def test_rejects_token_with_disallowed_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-1: a valid token whose role is not in the allow-list is rejected."""
    priv_pem, pub_b64 = _keypair()
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", pub_b64)
    monkeypatch.setenv("BASTION_ALLOWED_ROLES", "service,worker")
    client = _make_app(demo_mode=False)
    # _token mints role="admin", which is not in the allow-list.
    assert (
        client.get("/protected", headers={"X-Platform-Token": _token(priv_pem)}).status_code == 401
    )
