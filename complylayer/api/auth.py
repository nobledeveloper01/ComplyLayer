"""Resolving an API key to exactly one tenant and one role.

§8.1's first isolation layer, and the one the other two exist to back up. A key
resolves to one tenant; the query layer scopes every read to that tenant; RLS
returns nothing if the query layer is ever bypassed. Three layers because one is
a single point of failure.

**Argon2id is deliberately slow, which is right for a password and wrong for a
per-request hot path.** Verifying it costs tens of milliseconds at sane
parameters — half the decision budget. So a successful verification is cached
in-process for 60 seconds.

Three things about that cache, each of which was wrong once:

**It is keyed on the whole key, not on the prefix.** It used to be keyed on
`presented[:16]`, and returned the cached credentials without comparing the
secret to anything. The prefix is stored in the clear precisely so a dashboard
can show which key is which — so for the life of every cache entry the effective
credential was a 16-character public string rather than a 192-bit secret, and
anyone who had seen a prefix could authenticate as that tenant. Proven with a
forged key before it was changed. The digest below is SHA-256 of the presented
key: fast, because it is a lookup key rather than storage, and a hit is only
possible for a caller who supplied the identical full secret. The database still
holds Argon2id.

**Only the verification is cached, never the key's existence or its state.**
Every request re-reads the row. That is one indexed lookup on a unique column,
around a third of a millisecond against a 3 ms auth budget, and it is what makes
revocation take effect immediately rather than eventually. The expensive part
was never the query.

**Revocation is not a cache expiry.** `revoked_at` is read on the request, so a
revoked key stops working on its next request in every worker at once. The
earlier design cached the whole credential and offered `revoke_from_cache` as
the mechanism — which nothing outside the tests ever called, so a revoked key
kept authenticating for up to a minute, and for different lengths of time in
different workers.

The row is read through `complylayer_resolve_api_key`, not the ORM. See
migration 0008: this is the one lookup that cannot be scoped by tenant, because
it is the lookup that determines the tenant.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from django.db import connection

from complylayer.tenancy import Actor, Role

# Format: cl_{env}_{random}. The prefix is everything up to and including a short
# identifying fragment, stored in the clear so a lookup is one indexed query.
# It identifies a key. It does not authenticate one.
PREFIX_LENGTH = 16
SECRET_BYTES = 24
KEY_CACHE_TTL_SECONDS = 60.0

_hasher = PasswordHasher()

# digest of the full presented key -> (expires_at, prefix). The prefix is carried
# so revocation can evict by it; the value deliberately holds no credentials,
# because credentials are rebuilt from the row on every request.
_verified: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()

RESOLVE_SQL = (
    "SELECT id, tenant_id, hashed_secret, environment, role, revoked_at "
    "FROM complylayer_resolve_api_key(%s)"
)


class AuthenticationFailed(Exception):
    """Deliberately one exception for every failure mode.

    An unknown key, a wrong secret and a revoked key all produce the same
    message. Distinguishing them would tell somebody probing which of their
    guesses was closer.
    """


@dataclass(frozen=True)
class Credentials:
    tenant_id: str
    key_id: str
    environment: str
    actor: Actor


def generate_key(environment: str = "live") -> tuple[str, str]:
    """Return (full_key, prefix). The full key is shown once and never stored."""
    secret = secrets.token_urlsafe(SECRET_BYTES)
    full = f"cl_{environment}_{secret}"
    return full, full[:PREFIX_LENGTH]


def hash_secret(full_key: str) -> str:
    return _hasher.hash(full_key)


def _digest(presented: str) -> str:
    """The cache key: the whole presented key, never a fragment of it."""
    return hashlib.sha256(presented.encode()).hexdigest()


def clear_cache() -> None:
    with _cache_lock:
        _verified.clear()


def revoke_from_cache(prefix: str) -> None:
    """Drop any remembered verification for a key.

    Correctness no longer depends on this — `revoked_at` is read on every
    request — but a revoked key should not leave a usable verification behind
    either, and this keeps the memory from outliving the key.
    """
    with _cache_lock:
        for digest in [d for d, (_, p) in _verified.items() if p == prefix]:
            del _verified[digest]


def _resolve(prefix: str) -> tuple | None:
    """The one lookup that runs before any tenant is known (migration 0008)."""
    with connection.cursor() as cursor:
        cursor.execute(RESOLVE_SQL, [prefix])
        return cursor.fetchone()


def authenticate(header_value: str, now: float | None = None) -> Credentials:
    """Resolve `Authorization: Bearer cl_live_...` to a tenant and an actor."""
    moment = time.monotonic() if now is None else now

    if not header_value or not header_value.startswith("Bearer "):
        raise AuthenticationFailed("Send an API key as `Authorization: Bearer cl_live_...`.")

    presented = header_value.removeprefix("Bearer ").strip()
    if not presented.startswith("cl_"):
        raise AuthenticationFailed("That is not a ComplyLayer API key.")

    prefix = presented[:PREFIX_LENGTH]

    # Read every time. The cache below skips the Argon2id verification, which is
    # the expensive part; it must never skip finding out whether the key still
    # exists and is still live.
    row = _resolve(prefix)
    if row is None:
        raise AuthenticationFailed("That API key is not valid.")

    key_id, tenant_id, hashed_secret, environment, role, revoked_at = row
    if revoked_at is not None:
        raise AuthenticationFailed("That API key is not valid.")

    digest = _digest(presented)
    with _cache_lock:
        remembered = _verified.get(digest)
    fresh = remembered is not None and remembered[0] > moment

    if not fresh:
        try:
            _hasher.verify(hashed_secret, presented)
        except (VerifyMismatchError, VerificationError):
            raise AuthenticationFailed("That API key is not valid.") from None
        with _cache_lock:
            _verified[digest] = (moment + KEY_CACHE_TTL_SECONDS, prefix)

    return Credentials(
        tenant_id=tenant_id,
        key_id=key_id,
        environment=environment,
        actor=Actor(id=key_id, role=Role(role)),
    )
