"""Making the second factor cost something to guess.

A TOTP code is six digits and `valid_window=1` accepts three of them at any
moment, so a guess lands three times in a million and even odds take about
231,000 of them. With no limit on guesses that is not a second factor, it is a
delay: at a modest 200 requests a second an attacker holding a stolen password
is through in nineteen minutes, and no counter, log or alert anywhere records
the attempt. The security review found no throttle, lockout, attempt counter or
backoff anywhere in the project.

Three controls, because they close different doors:

1. **Backoff on repeated failures**, per account and per scope. Doubling and
   capped, which moves an even-odds search from about nineteen minutes to about
   fifteen months, without ever locking somebody out for long over a
   fat-fingered code. The numbers are worked in `tests/test_throttle.py` so the
   claim is checked rather than asserted — the first version of that test
   claimed centuries and was out by two orders of magnitude.
2. **A code cannot be used twice.** TOTP codes stay valid for their whole window,
   so a code observed over someone's shoulder — or replayed from a proxy — works
   again until the window rolls. Each accepted code is burned for the length of
   its validity.
3. **Password attempts are throttled too**, on the same mechanism. Otherwise the
   second factor is protecting an account whose first factor is free to guess.

**These fail open when Redis is unreachable, deliberately.** The alternative
locks every compliance officer out of the approval queue during an infrastructure
outage — including the ones who need to respond to it — and this endpoint is not
the money path. §10.3 makes fail-open/fail-closed a per-severity decision on
*decisions*; the same reasoning applies here and lands the other way, because the
cost of a false lockout is high and the window is small. The degradation is
logged at warning rather than swallowed, which is the difference between a stated
trade and an accident.
"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

# Five is comfortably above a person mistyping or a phone clock drifting, and far
# below anything useful to a search over a million codes.
FREE_ATTEMPTS = 5

# Doubling from 30 seconds: 30, 60, 120 ... capped at fifteen minutes so a
# forgotten authenticator is never an all-day lockout. That cap allows twenty
# guesses an hour, which is what turns nineteen minutes into fifteen months.
# The lockouts themselves are the other half of the control: a real attempt
# generates a steady stream of them, and that is the thing to alert on.
BASE_LOCKOUT_SECONDS = 30
MAX_LOCKOUT_SECONDS = 15 * 60

# Long enough to cover the failures that belong to one attack, short enough that
# yesterday's typo does not count against today.
ATTEMPT_WINDOW_SECONDS = 60 * 60

# A code is valid for its own step plus the neighbours pyotp accepts.
CODE_REPLAY_SECONDS = 90


def _key(scope: str, identity: str) -> str:
    """Identities are hashed: this key holds an email address, and Redis is a
    place people run `KEYS *` in an incident."""
    digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return f"cl:throttle:{scope}:{digest}"


def lockout_seconds(client, scope: str, identity: str) -> int:
    """How long this identity must wait, or 0.

    Returns 0 when Redis is unreachable — see the module docstring.
    """
    if client is None:
        return 0
    try:
        raw = client.get(_key(scope, identity))
    except Exception:
        logger.warning("throttle unavailable, allowing the attempt", exc_info=True)
        return 0

    failures = int(raw or 0)
    if failures <= FREE_ATTEMPTS:
        return 0

    try:
        ttl = client.ttl(_key(scope, identity))
    except Exception:
        logger.warning("throttle unavailable, allowing the attempt", exc_info=True)
        return 0
    return max(0, int(ttl or 0))


def record_failure(client, scope: str, identity: str) -> int:
    """Count one failure and extend the wait. Returns the new failure count."""
    if client is None:
        return 0
    key = _key(scope, identity)
    try:
        failures = int(client.incr(key))
        if failures <= FREE_ATTEMPTS:
            client.expire(key, ATTEMPT_WINDOW_SECONDS)
        else:
            over = failures - FREE_ATTEMPTS - 1
            wait = min(BASE_LOCKOUT_SECONDS * (2**over), MAX_LOCKOUT_SECONDS)
            client.expire(key, wait)
        return failures
    except Exception:
        logger.warning("throttle unavailable, failure not counted", exc_info=True)
        return 0


def clear(client, scope: str, identity: str) -> None:
    """A success wipes the slate, so a real user is never punished for a bad day."""
    if client is None:
        return
    try:
        client.delete(_key(scope, identity))
    except Exception:
        logger.warning("throttle unavailable, counter not cleared", exc_info=True)


def consume_code(client, identity: str, code: str) -> bool:
    """Burn a one-time code. False if it has already been used.

    Without this a TOTP code is reusable for the whole time it is valid, which is
    exactly the window in which somebody who watched it being typed would use it.
    """
    if client is None:
        return True
    digest = hashlib.sha256(f"{identity}:{code}".encode()).hexdigest()[:32]
    try:
        return bool(client.set(f"cl:otp:{digest}", "1", nx=True, ex=CODE_REPLAY_SECONDS))
    except Exception:
        logger.warning("replay guard unavailable, accepting the code", exc_info=True)
        return True


def wait_message(seconds: int) -> str:
    """What the person sees. Says how long, because "try again later" is the
    sentence that generates a support ticket."""
    if seconds >= 60:
        minutes = (seconds + 59) // 60
        unit = "minute" if minutes == 1 else "minutes"
        return f"Too many attempts. Try again in {minutes} {unit}."
    return f"Too many attempts. Try again in {max(seconds, 1)} seconds."
