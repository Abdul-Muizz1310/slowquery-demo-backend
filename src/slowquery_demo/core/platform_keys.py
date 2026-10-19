"""The trusted platform-token keys, from configuration (spec 14).

* bastion's key: ``BASTION_SIGNING_KEY_PUBLIC`` (base64 SPKI DER), else the
  JSON field ``publicKey`` fetched from ``BASTION_PUBLIC_KEY_URL`` and cached
  for an hour (asynchronously, so the event loop never blocks on it);
* the dashboard frontend's key: ``SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC``.

Values are cleaned of a byte-order mark and edge whitespace. A value that is
not an Ed25519 public key is ignored with a WARNING naming the variable, never
the value.
"""

from __future__ import annotations

import base64
import binascii
import functools
import logging
import time
from typing import Final

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from slowquery_demo.core.config import Settings
from slowquery_demo.core.platform_token import BASTION_ISS, FRONTEND_ISS, TrustedKey

logger = logging.getLogger(__name__)

BASTION_KEY_ENV: Final = "BASTION_SIGNING_KEY_PUBLIC"
BASTION_KEY_URL_ENV: Final = "BASTION_PUBLIC_KEY_URL"
FRONTEND_KEY_ENV: Final = "SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC"

_URL_KEY_TTL_S: Final = 3600.0
_FETCH_TIMEOUT_S: Final = 5.0

# (fetched_at_monotonic, url, key): only for the BASTION_PUBLIC_KEY_URL path.
_url_cache: tuple[float, str, Ed25519PublicKey] | None = None


def clean_key_value(raw: str | None) -> str:
    """Strip edge whitespace and a byte-order mark (values pasted from a shell)."""
    return (raw or "").strip().lstrip("﻿").strip()


def parse_ed25519_public_key(b64_der: str) -> Ed25519PublicKey | None:
    """Parse base64 SPKI DER into an Ed25519 public key; ``None`` for anything else."""
    try:
        key = serialization.load_der_public_key(base64.b64decode(b64_der, validate=True))
    except (ValueError, binascii.Error, UnsupportedAlgorithm):
        return None
    return key if isinstance(key, Ed25519PublicKey) else None


@functools.lru_cache(maxsize=16)
def _parse_named(source: str, value: str) -> Ed25519PublicKey | None:
    """Parse one configured value; cached so a bad value warns once, not per request."""
    key = parse_ed25519_public_key(value)
    if key is None:
        logger.warning("%s is not a base64 Ed25519 public key (SPKI DER); ignoring it", source)
    return key


def reset_key_cache() -> None:
    """Forget the fetched key and the parsed values (test seam)."""
    global _url_cache
    _url_cache = None
    _parse_named.cache_clear()


async def _fetch_bastion_key(url: str) -> Ed25519PublicKey | None:
    global _url_cache
    now = time.monotonic()
    if _url_cache is not None:
        fetched_at, cached_url, cached_key = _url_cache
        if cached_url == url and now - fetched_at < _URL_KEY_TTL_S:
            return cached_key
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        value = clean_key_value(str(resp.json()["publicKey"]))
    except Exception as exc:
        logger.warning("could not fetch %s: %s", BASTION_KEY_URL_ENV, type(exc).__name__)
        return None
    key = _parse_named(BASTION_KEY_URL_ENV, value)
    if key is not None:
        _url_cache = (now, url, key)
    return key


async def load_trusted_keys(settings: Settings) -> list[TrustedKey]:
    """Every usable key, bastion's first. Empty means "not configured" (fail closed)."""
    keys: list[TrustedKey] = []

    bastion_value = clean_key_value(settings.bastion_signing_key_public)
    bastion_url = clean_key_value(settings.bastion_public_key_url)
    bastion: Ed25519PublicKey | None = None
    if bastion_value:
        bastion = _parse_named(BASTION_KEY_ENV, bastion_value)
    elif bastion_url:
        bastion = await _fetch_bastion_key(bastion_url)
    if bastion is not None:
        keys.append(TrustedKey(issuer=BASTION_ISS, key=bastion))

    frontend_value = clean_key_value(settings.slowquery_frontend_signing_key_public)
    if frontend_value:
        frontend = _parse_named(FRONTEND_KEY_ENV, frontend_value)
        if frontend is not None:
            keys.append(TrustedKey(issuer=FRONTEND_ISS, key=frontend))
    return keys
