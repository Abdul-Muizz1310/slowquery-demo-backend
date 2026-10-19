"""Platform token contract v2: the pure verification core (spec 14).

The workspace contract (``docs/specs/platform-contract-v2.md`` in the
meta-repo, DR-0033) fixes the rules; this is slowquery-demo-backend's copy
(DR-0006). Nothing here reads the environment, the clock or the network: the
trusted keys and ``now`` are arguments, so every rule is unit-testable.

A token is valid iff its header ``alg`` is ``EdDSA``, its signature verifies
against one trusted key, ``iss`` is the issuer *that key* admits (plus
``role == "frontend"`` for the frontend key), ``aud`` is the string
``"slowquery"``, ``exp``/``iat`` are integers within a 10 s leeway with a
lifetime of at most 300 s, and ``sub`` is a non-empty string.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, TypeGuard

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SERVICE_ID: Final = "slowquery"
BASTION_ISS: Final = "bastion"
FRONTEND_ISS: Final = "slowquery-frontend"
FRONTEND_ROLE: Final = "frontend"
LEEWAY_S: Final = 10
MAX_LIFETIME_S: Final = 300

Issuer = Literal["bastion", "slowquery-frontend"]


class TokenCheck(enum.StrEnum):
    """The check a rejected token failed; named in the WARNING log."""

    malformed = "malformed"
    alg = "alg"
    signature = "signature"
    iss = "iss"
    role = "role"
    aud = "aud"
    exp = "exp"
    iat = "iat"
    lifetime = "lifetime"
    sub = "sub"


class InvalidPlatformTokenError(Exception):
    """A present token failed ``check``. Never carries the token itself."""

    def __init__(self, check: TokenCheck) -> None:
        super().__init__(check.value)
        self.check = check


@dataclass(frozen=True, slots=True)
class Principal:
    """Who an authenticated request is: the rate-limit bucket is ``iss:sub``."""

    iss: Issuer
    sub: str
    role: str | None

    @property
    def bucket(self) -> str:
        return f"{self.iss}:{self.sub}"


@dataclass(frozen=True, slots=True)
class TrustedKey:
    """A public key and the one issuer it may vouch for."""

    issuer: Issuer
    key: Ed25519PublicKey


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def check_claims(claims: Mapping[str, Any], *, issuer: Issuer, now: float) -> Principal:
    """Validate the claims of a token whose signature ``issuer``'s key verified."""
    if claims.get("iss") != issuer:
        raise InvalidPlatformTokenError(TokenCheck.iss)
    role = claims.get("role")
    if issuer == FRONTEND_ISS and role != FRONTEND_ROLE:
        raise InvalidPlatformTokenError(TokenCheck.role)
    aud = claims.get("aud")
    if not isinstance(aud, str) or aud != SERVICE_ID:
        raise InvalidPlatformTokenError(TokenCheck.aud)
    exp, iat = claims.get("exp"), claims.get("iat")
    if not _is_int(exp):
        raise InvalidPlatformTokenError(TokenCheck.exp)
    if not _is_int(iat):
        raise InvalidPlatformTokenError(TokenCheck.iat)
    if not exp + LEEWAY_S > now:
        raise InvalidPlatformTokenError(TokenCheck.exp)
    if not iat - LEEWAY_S <= now:
        raise InvalidPlatformTokenError(TokenCheck.iat)
    if not 0 < exp - iat <= MAX_LIFETIME_S:
        raise InvalidPlatformTokenError(TokenCheck.lifetime)
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise InvalidPlatformTokenError(TokenCheck.sub)
    return Principal(iss=issuer, sub=sub, role=role if isinstance(role, str) else None)


def _verified_claims(token: str, key: Ed25519PublicKey) -> dict[str, Any] | None:
    """The token's claims if ``key`` verifies its EdDSA signature, else ``None``."""
    try:
        payload = jwt.PyJWS().decode_complete(token, key, algorithms=["EdDSA"])["payload"]
    except (jwt.InvalidTokenError, ValueError, TypeError):
        return None
    try:
        claims = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidPlatformTokenError(TokenCheck.malformed) from exc
    if not isinstance(claims, dict):
        raise InvalidPlatformTokenError(TokenCheck.malformed)
    return claims


def verify_platform_token(token: str, keys: Sequence[TrustedKey], *, now: float) -> Principal:
    """The token's principal, or :class:`InvalidPlatformTokenError` naming the failed check.

    The first key whose signature check passes decides which issuer is
    allowed; the ``iss`` claim never picks the key.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise InvalidPlatformTokenError(TokenCheck.malformed) from exc
    if header.get("alg") != "EdDSA":
        raise InvalidPlatformTokenError(TokenCheck.alg)
    for trusted in keys:
        claims = _verified_claims(token, trusted.key)
        if claims is not None:
            return check_claims(claims, issuer=trusted.issuer, now=now)
    raise InvalidPlatformTokenError(TokenCheck.signature)
