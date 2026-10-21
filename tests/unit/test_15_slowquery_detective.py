"""Spec 15: the demo on slowquery-detective 0.2 (unit lane).

Drives ``create_app()`` and the pieces it wires: the library's install, the
contract-v2 ``authorize`` hook, the one observed engine that a branch switch
repoints, the uninstrumented health probe, and the demo's own stream and
force-explain routes on top of the library store. No database: the store is
replaced by the library's in-memory store where a test reads or writes.
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

FREE_MODELS = (
    "google/gemma-4-26b-a4b-it:free",
    "minimax/minimax-m3.1-flash:free",
    "google/gemma-4-31b-it:free",
)
FP = "7f9c44d378fe11e2"


def _app(**env: str) -> Any:
    import os

    from slowquery_demo.main import create_app

    for key, value in env.items():
        os.environ[key] = value
    return create_app()


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "LLM_FALLBACK_ENABLED",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL_PRIMARY",
        "OPENROUTER_MODEL_FAST",
        "OPENROUTER_MODEL_FALLBACK",
        "SLOWQUERY_STORE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


# --- 1-3: wiring ---------------------------------------------------------------


def test_01_installs_the_library_once_with_the_store_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from slowquery_detective import PostgresStoreWriter

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@slow.example/db")
    app = _app()
    assert app.state.slowquery_installed is True
    assert app.state.slowquery_runtime.service == "slowquery"
    store = app.state.slowquery_store
    assert isinstance(store, PostgresStoreWriter)
    assert "slow.example" in store.url

    monkeypatch.setenv("SLOWQUERY_STORE_URL", "postgresql://u:p@store.example/db")
    assert "store.example" in _app().state.slowquery_store.url


def test_02_routes_under_the_mount_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserted over HTTP: FastAPI mounts included routers lazily, so the
    route table is not a reliable thing to inspect."""
    import slowquery_demo.api.routers.dashboard as dash

    async def _one_frame(_request: object) -> Any:
        yield 'data: {"kind": "heartbeat", "now": "2026-10-20T00:00:00Z"}\n\n'

    monkeypatch.setattr(dash, "_sse_generator", _one_frame)
    app = _app()
    _with_memory_store(app)
    client = TestClient(app)

    assert client.get("/_slowquery/queries").status_code == 200  # the library's list
    assert client.get(f"/_slowquery/queries/{FP}").status_code == 404  # the library's detail
    assert client.get("/_slowquery/queries/NOT-HEX").status_code == 404
    stream = client.get("/_slowquery/api/stream")  # the demo's stream
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    # The demo's force-explain: reached (its admin gate answers), not 404/405.
    assert client.post(f"/_slowquery/queries/{FP}/force-explain").status_code == 403
    # No top-level copy and no 0.1 route.
    assert client.get("/queries").status_code == 404
    assert client.get("/_slowquery/api/queries").status_code == 404
    assert client.post(f"/_slowquery/api/queries/{FP}/apply").status_code == 404


def test_03_llm_off_by_default_and_free_models_by_default() -> None:
    from slowquery_demo.core.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_fallback_enabled is False
    assert (
        settings.openrouter_model_primary,
        settings.openrouter_model_fast,
        settings.openrouter_model_fallback,
    ) == FREE_MODELS
    worker = _app().state.slowquery_worker
    assert worker._explainer is None


def test_03_llm_enabled_without_key_fails_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    from slowquery_demo.core.errors import ConfigError

    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        _app()


def test_03_llm_enabled_with_key_builds_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from slowquery_demo.core.config import Settings
    from slowquery_demo.core.observability import build_llm_config

    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    config = build_llm_config(Settings(_env_file=None))  # type: ignore[call-arg]
    assert config.enabled is True
    assert config.api_key is not None and config.api_key.get_secret_value() == "sk-or-test"
    assert (config.model_primary, config.model_fast, config.model_fallback) == FREE_MODELS
    assert _app().state.slowquery_worker._explainer is not None


# --- 4-5: authorize --------------------------------------------------------------


async def test_04_platform_admitted_reads_the_middleware_verdict() -> None:
    from starlette.requests import Request

    from slowquery_demo.core.observability import platform_admitted

    def request(state: dict[str, Any]) -> Request:
        return Request({"type": "http", "headers": [], "state": state})

    assert await platform_admitted(request({"platform_admitted": True})) is True
    assert await platform_admitted(request({})) is False
    assert await platform_admitted(request({"platform_admitted": "yes"})) is False


def _keypair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return pem, base64.b64encode(der).decode()


def _token(pem: str, **claims: Any) -> str:
    now = int(time.time())
    body = {
        "iss": "bastion",
        "aud": "slowquery",
        "sub": "v_abcdefghij",
        "role": "viewer",
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
    }
    body.update(claims)
    return jwt.encode(body, pem, algorithm="EdDSA")


def _with_memory_store(app: Any) -> None:
    from slowquery_detective import InMemoryStoreWriter

    store = InMemoryStoreWriter()
    app.state.slowquery_runtime.store = store
    app.state.slowquery_store = store


def test_05_library_routes_follow_contract_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    from slowquery_demo.core import platform_keys

    bastion_pem, bastion_pub = _keypair()
    front_pem, front_pub = _keypair()
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("BASTION_SIGNING_KEY_PUBLIC", bastion_pub)
    monkeypatch.setenv("SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC", front_pub)
    platform_keys.reset_key_cache()
    app = _app()
    _with_memory_store(app)
    client = TestClient(app)

    assert client.get("/_slowquery/queries").status_code == 401
    ok = client.get("/_slowquery/queries", headers={"X-Platform-Token": _token(bastion_pem)})
    assert ok.status_code == 200, ok.text
    assert ok.json() == []
    frontend = _token(
        front_pem, iss="slowquery-frontend", sub="slowquery-frontend", role="frontend"
    )
    assert (
        client.get("/_slowquery/queries", headers={"X-Platform-Token": frontend}).status_code == 200
    )
    platform_keys.reset_key_cache()


def test_05_demo_mode_admits_anonymous_reads() -> None:
    app = _app()  # test settings: DEMO_MODE=true, no key
    _with_memory_store(app)
    resp = TestClient(app).get("/_slowquery/queries?request_id=R-1")
    assert resp.status_code == 200
    assert resp.json() == []


def test_05_library_route_without_the_middleware_verdict_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import slowquery_demo.core.platform_auth as auth

    # A request that reaches the route without the contract-v2 admission.
    monkeypatch.setattr(auth, "is_public", lambda method, path: True)
    app = _app()
    _with_memory_store(app)
    assert TestClient(app).get("/_slowquery/queries").status_code == 403


# --- 6-8: one observed engine ----------------------------------------------------


def test_06_commerce_engine_instrumented_health_engine_not() -> None:
    from slowquery_detective import hooks

    app = _app()
    assert hooks.is_attached(app.state.engine)
    assert not hooks.is_attached(app.state.health_engine)
    assert app.state.engine is not app.state.health_engine


class _FakeTarget:
    def __init__(self) -> None:
        self.checked: list[str] = []


async def test_07_switch_repoints_without_replacing_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from slowquery_detective import hooks

    import slowquery_demo.main as main_mod

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@slow.example/db")
    monkeypatch.setenv("DATABASE_URL_FAST", "postgresql://u:p@fast.example/db")
    app = _app()
    engine = app.state.engine
    checked: list[str] = []

    async def _check(url: str) -> None:
        checked.append(url)

    monkeypatch.setattr(main_mod, "check_branch_reachable", _check)
    disposed: list[str] = []
    monkeypatch.setattr(
        type(engine), "dispose", lambda self, close=True: _record(disposed, self), raising=False
    )
    app.state.slowquery_buffer.record(FP, 1200.0)

    await app.state.branch_switcher.switch("fast")

    assert checked == ["postgresql://u:p@fast.example/db"]
    assert app.state.branch_target.url == "postgresql://u:p@fast.example/db"
    assert app.state.engine is engine
    assert hooks.is_attached(app.state.engine)
    assert len(disposed) == 2  # the commerce pool and the health pool
    assert app.state.slowquery_buffer.keys() == frozenset()


async def _record(sink: list[str], engine: object) -> None:
    sink.append(repr(engine))


async def test_07_failed_check_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import slowquery_demo.main as main_mod

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@slow.example/db")
    app = _app()
    before = app.state.branch_target.url

    async def _down(url: str) -> None:
        raise ConnectionError("neon asleep")

    monkeypatch.setattr(main_mod, "check_branch_reachable", _down)
    app.state.slowquery_buffer.record(FP, 1200.0)
    with pytest.raises(ConnectionError):
        await app.state.branch_switcher.switch("fast")
    assert app.state.branch_target.url == before
    assert app.state.branch_switcher.active == "slow"
    assert FP in app.state.slowquery_buffer.keys()  # noqa: SIM118 - RingBuffer.keys()


def test_08_initial_target_is_the_persisted_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from slowquery_demo.core.branch_state import save_branch

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@slow.example/db")
    monkeypatch.setenv("DATABASE_URL_FAST", "postgresql://u:p@fast.example/db")
    save_branch("fast")
    app = _app()
    assert app.state.branch_switcher.active == "fast"
    assert app.state.branch_target.url == "postgresql://u:p@fast.example/db"


# --- 9: the stream reads the library store ----------------------------------------


async def test_09_stream_polls_the_library_store() -> None:
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from slowquery_detective.recorder import Batch, FingerprintDelta

    from slowquery_demo.api.routers.dashboard import _poll_fingerprints

    app = _app()
    _with_memory_store(app)
    now = datetime.now(UTC).timestamp()
    await app.state.slowquery_store.start()
    await app.state.slowquery_store.write_batch(
        "slowquery",
        Batch(
            fingerprints={
                FP: FingerprintDelta(
                    canonical_sql="select 1",
                    calls=3,
                    total_ms=30.0,
                    max_ms=12.0,
                    first_seen=now,
                    last_seen=now,
                    p50_ms=9.0,
                    p95_ms=11.0,
                    p99_ms=12.0,
                )
            }
        ),
    )
    request = MagicMock()
    request.app = app
    rows = await _poll_fingerprints(request)
    assert [(r.id, r.p95_ms) for r in rows] == [(FP, 11.0)]


async def test_09_stream_survives_an_unavailable_store() -> None:
    from unittest.mock import MagicMock

    from slowquery_demo.api.routers.dashboard import _poll_fingerprints

    app = _app()
    app.state.slowquery_runtime.ensure_ready = AsyncMock(return_value=False)
    request = MagicMock()
    request.app = app
    assert await _poll_fingerprints(request) == []


# --- 10: force-explain on the library store -----------------------------------------


def _force(app: Any, fp: str = FP) -> Any:
    return TestClient(app, raise_server_exceptions=False).post(
        f"/_slowquery/queries/{fp}/force-explain", headers={"X-Admin-Token": "s3cret"}
    )


def test_10_force_explain_unknown_fingerprint_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MUTATION_TOKEN", "s3cret")
    app = _app()
    _with_memory_store(app)
    assert _force(app).status_code == 404


def test_10_force_explain_store_unavailable_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MUTATION_TOKEN", "s3cret")
    app = _app()
    app.state.slowquery_runtime.ensure_ready = AsyncMock(return_value=False)
    assert _force(app).status_code == 503


# --- 6b: the connector dials the target, Neon URLs included ------------------------


class _FakeConn:
    def __init__(self, *, fail: bool = False) -> None:
        self.closed = False
        self._fail = fail

    async def fetchval(self, query: str) -> int:
        if self._fail:
            raise ConnectionError("query failed")
        return 1

    async def close(self) -> None:
        self.closed = True


async def test_06b_connector_dials_the_current_target(monkeypatch: pytest.MonkeyPatch) -> None:
    import slowquery_demo.core.database as database

    dialed: list[tuple[str, float]] = []

    async def _connect(*, dsn: str, timeout: float) -> _FakeConn:
        dialed.append((dsn, timeout))
        return _FakeConn()

    monkeypatch.setattr(database.asyncpg, "connect", _connect)
    target = database.BranchTarget(
        "postgresql://u:p@ep-slow.neon.tech/db?sslmode=require&channel_binding=require"
    )
    connect = database._connector(target)
    await connect()
    target.url = "postgresql+asyncpg://u:p@ep-fast.neon.tech/db"
    await connect()
    assert dialed[0][0] == "postgresql://u:p@ep-slow.neon.tech/db?sslmode=require"
    assert dialed[1][0] == "postgresql://u:p@ep-fast.neon.tech/db"
    assert all(timeout > 0 for _, timeout in dialed)


@pytest.mark.parametrize("fail", [False, True])
async def test_06b_reachability_check_always_closes(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    import slowquery_demo.core.database as database

    conn = _FakeConn(fail=fail)

    async def _connect(*, dsn: str, timeout: float) -> _FakeConn:
        return conn

    monkeypatch.setattr(database.asyncpg, "connect", _connect)
    if fail:
        with pytest.raises(ConnectionError):
            await database.check_branch_reachable("postgresql://u:p@h/db")
    else:
        await database.check_branch_reachable("postgresql://u:p@h/db")
    assert conn.closed


def test_06b_branch_target_needs_a_url() -> None:
    from slowquery_demo.core.database import BranchTarget

    with pytest.raises(ValueError):
        BranchTarget("")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql://u:p@h/db?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@h/db?ssl=require",
        ),
        ("postgresql+asyncpg://u:p@h/db?ssl=require", "postgresql+asyncpg://u:p@h/db?ssl=require"),
        ("postgresql://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
    ],
)
def test_06b_alembic_url_is_normalised_for_asyncpg(url: str, expected: str) -> None:
    from slowquery_demo.core.db_config import normalise_asyncpg_url

    assert normalise_asyncpg_url(url) == expected
