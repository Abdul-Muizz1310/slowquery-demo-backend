# Spec 14 — Platform token contract v2

## Goal

Replace the opt-in, fail-open bastion token check (and its `BASTION_EXPECTED_*` env knobs) with the
workspace **platform-token contract v2** (`docs/specs/platform-contract-v2.md` in the meta-repo, DR-0033).
This file is slowquery-demo-backend's reading of it; where the two differ, the workspace contract wins.

Service id: **`slowquery`** (a constant in `core/platform_token.py`, never configuration).

## Trusted keys

| Key | Env | Admits |
|---|---|---|
| bastion | `BASTION_SIGNING_KEY_PUBLIC` (base64 SPKI DER), else the JSON field `publicKey` fetched from `BASTION_PUBLIC_KEY_URL` and cached for an hour | `iss == "bastion"` |
| slowquery-dashboard-frontend | `SLOWQUERY_FRONTEND_SIGNING_KEY_PUBLIC` (base64 SPKI DER) | `iss == "slowquery-frontend"` and `role == "frontend"` |

Values are stripped of a byte-order mark and edge whitespace first. A value that is not an Ed25519 SPKI key
is ignored with a WARNING that names the variable (never the value). **The key decides the issuer**: a token
signed by the frontend key that claims `iss "bastion"` (or the reverse) is invalid.

## Verification

A token is read from `X-Platform-Token`, else from `Authorization: Bearer <token>`. It is valid iff:

1. it is a compact JWS whose header `alg` is `EdDSA` (`none`, `HS256`, anything else ⇒ invalid);
2. its signature verifies against one trusted key, and `iss` is the one that key admits (plus
   `role == "frontend"` for the frontend key);
3. `aud` is the string `"slowquery"` (missing, another id, or a list ⇒ invalid);
4. `exp` and `iat` are integers with `exp + 10 > now`, `iat - 10 <= now`, and `0 < exp - iat <= 300`;
5. `sub` is a non-empty string (the rate-limit bucket needs it).

The principal `(iss, sub, role)` of a valid token is stored on `request.state.platform_principal`.

## Decisions per request

| Route | Token | `DEMO_MODE` | Keys | Answer |
|---|---|---|---|---|
| public | any | any | any | handled normally, never 401/503 from auth |
| protected | any | any | none configured, `APP_ENV != "test"` | `503 {"detail":"platform_auth_unconfigured"}` |
| protected | missing | `false` (default) | ≥ 1 | `401 {"detail":"missing_platform_token"}` |
| protected | missing | `true` | ≥ 1, or test settings | proceeds anonymous; WARNING logged |
| protected | present, invalid | any | any | `401 {"detail":"invalid_platform_token"}`; WARNING names the failed check |
| protected | present, valid | any | ≥ 1 | proceeds as the principal |

Under the test settings (`APP_ENV=test`) the 503 does not apply; a missing token is then decided by
`DEMO_MODE` as above, and a present token can only be invalid (there is no key to verify it).

**Public routes:** `/health`, `/version`, `/metrics`, `/openapi.json`, `/docs`, `/redoc` (and their
sub-paths), `/` (a redirect to `/docs`), and the read-only commerce routes for `GET`/`HEAD` only:
`/users`, `/users/{id}`, `/users/{id}/orders`, `/products`, `/products/{id}`, `/orders`, `/orders/{id}`,
`/order_items`. **Protected:** everything else, in particular `/_slowquery/*` (including the SSE stream
`/_slowquery/api/stream` and `POST …/force-explain`) and `/branches/*` (`POST /branches/switch`,
`GET /branches/current`), and any non-`GET` request to a commerce path.

`DEMO_MODE` defaults to `false`. The mutation gates of spec 11 (cooldown, `DEMO_MUTATION_TOKEN`) still
apply on top of authentication.

**Rate limiting.** The spec-11 cooldown buckets on `"{iss}:{sub}"` for an authenticated request, else on
the leftmost `X-Forwarded-For` address, else the socket peer. uvicorn runs with
`--proxy-headers --forwarded-allow-ips='*'` behind Render.

**CORS.** Allowed request headers are exactly `x-platform-token`, `x-request-id`, `content-type`,
`authorization` and `x-admin-token`. CORS is the outermost middleware, so a preflight never reaches the
token check and a 401 still carries the CORS headers.

**Removed.** `BASTION_EXPECTED_ISSUER`, `BASTION_EXPECTED_AUDIENCE`, `BASTION_EXPECTED_SERVICE` and
`BASTION_ALLOWED_ROLES` are no longer read; the contract fixes all four.

## Test cases

Conformance (the workspace contract's §3, numbered as there), against `create_app()` with throwaway keys:

1. valid bastion token, `aud "slowquery"` ⇒ authenticated (200 on `GET /branches/current`).
2. `aud` = another service's id (`paper-trail`, `slowquery-demo`) ⇒ 401 invalid.
3. `aud` missing ⇒ 401 invalid; `aud` as a list `["slowquery"]` ⇒ 401 invalid.
4. `iss "bastion"` signed with the frontend key ⇒ 401 invalid (and `iss "slowquery-frontend"` signed with
   bastion's key ⇒ 401 invalid).
5. frontend-key token with `role` ≠ `"frontend"` ⇒ 401 invalid.
6. expired 5 s ago ⇒ authenticated; expired 15 s ago ⇒ 401 invalid.
7. `iat` 5 s in the future ⇒ authenticated; 15 s in the future ⇒ 401 invalid.
8. `alg none` or `HS256` ⇒ 401 invalid.
9. tampered payload ⇒ 401 invalid.
10. no token, `DEMO_MODE=false` ⇒ 401 missing; no token, `DEMO_MODE=true` ⇒ proceeds, WARNING logged.
11. present-but-invalid token with `DEMO_MODE=true` ⇒ 401 invalid.
12. no trusted key (`APP_ENV` not `test`) ⇒ 503 on a protected route, 200 on `/health`.
13. every public route answers without a token.
14. the rate-limit key is `iss:sub` for an authenticated request.

slowquery-specific:

15. Every protected route group answers 401 missing without a token: `GET /_slowquery/queries`,
    `GET /_slowquery/queries/{id}`, `GET /_slowquery/api/stream`, `POST /_slowquery/queries/{id}/force-explain`,
    `POST /branches/switch`, `GET /branches/current`; and a `POST`/`DELETE` to a commerce path is never public.
16. A valid frontend token (`iss "slowquery-frontend"`, `role "frontend"`) is authenticated.
17. `Authorization: Bearer` is accepted when `X-Platform-Token` is absent; when both are present
    `X-Platform-Token` decides (an invalid one is 401 even beside a valid bearer).
18. `exp - iat > 300`, a non-integer `exp`/`iat` (float, string, boolean) and a missing or empty `sub` ⇒ 401
    invalid.
19. The WARNING for a rejected token names the check and never contains the token.
20. A malformed key value is ignored with a WARNING naming its variable and not its value; if no valid key is
    left, protected routes answer 503.
21. With `BASTION_SIGNING_KEY_PUBLIC` unset, bastion's key is fetched from `BASTION_PUBLIC_KEY_URL` once and
    cached; a failed fetch leaves bastion's key unconfigured.
22. Unauthenticated requests bucket on the leftmost `X-Forwarded-For` address, else the socket peer.
23. A CORS preflight asking for `x-platform-token, x-request-id, content-type` succeeds without a token and a
    401 carries `access-control-allow-origin`.
24. `Settings().demo_mode` is `False` when `DEMO_MODE` is unset.
25. `BASTION_EXPECTED_*` are ignored: with `BASTION_EXPECTED_AUDIENCE=paper-trail` set, a valid slowquery
    token is still authenticated.
26. An authenticated `GET /_slowquery/api/stream` streams `text/event-stream`.
27. The Dockerfile runs uvicorn with `--proxy-headers --forwarded-allow-ips=*`.
