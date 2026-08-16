"""Resolving an API key to exactly one tenant and one role.

§8.1's first isolation layer, and the one the other two exist to back up. A key
resolves to one tenant; the query layer scopes every read to that tenant; RLS
returns nothing if the query layer is ever bypassed. Three layers because one is
a single point of failure.

**Argon2id is deliberately slow, which is right for a password and wrong for a
per-request hot path.** Verifying it costs tens of milliseconds at sane
parameters — half the decision budget. So a successful verification is cached
in-process against the key prefix for 60 seconds, and revocation is published on
the same channel the rule cache uses rather than waiting for the cache to expire.
The TTL is the backstop, not the mechanism: a leaked key that stayed live for a
minute after revocation would be a real window.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from complylayer.tenancy import Actor, Role

# Format: cl_{env}_{random}. The prefix is everything up to and including a short
# identifying fragment, stored in the clear so a lookup is one indexed query.
PREFIX_LENGTH = 16
SECRET_BYTES = 24
KEY_CACHE_TTL_SECONDS = 60.0

_hasher = PasswordHasher()
_cache: dict[str, tuple[float, Credentials]] = {}
_cache_lock = threading.Lock()


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


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def revoke_from_cache(prefix: str) -> None:
    """Called on revocation, so the TTL never has to be the thing that stops a
    leaked key."""
    with _cache_lock:
        _cache.pop(prefix, None)


def authenticate(header_value: str, now: float | None = None) -> Credentials:
    """Resolve `Authorization: Bearer cl_live_...` to a tenant and an actor."""
    from complylayer.models import ApiKey

    moment = time.monotonic() if now is None else now

    if not header_value or not header_value.startswith("Bearer "):
        raise AuthenticationFailed("Send an API key as `Authorization: Bearer cl_live_...`.")

    presented = header_value.removeprefix("Bearer ").strip()
    if not presented.startswith("cl_"):
        raise AuthenticationFailed("That is not a ComplyLayer API key.")

    prefix = presented[:PREFIX_LENGTH]

    with _cache_lock:
        cached = _cache.get(prefix)
    if cached is not None:
        expires_at, credentials = cached
        if expires_at > moment:
            return credentials

    key = ApiKey.objects.filter(prefix=prefix).select_related("tenant").first()
    if key is None or not key.is_active:
        raise AuthenticationFailed("That API key is not valid.")

    try:
        _hasher.verify(key.hashed_secret, presented)
    except VerifyMismatchError:
        raise AuthenticationFailed("That API key is not valid.") from None

    credentials = Credentials(
        tenant_id=key.tenant_id,
        key_id=key.id,
        environment=key.environment,
        actor=Actor(id=key.id, role=Role(key.role)),
    )
    with _cache_lock:
        _cache[prefix] = (moment + KEY_CACHE_TTL_SECONDS, credentials)
    return credentials
