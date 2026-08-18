"""The refusal a server performs before it takes traffic.

Separate from `server/asgi.py` so the rule can be imported and tested without
performing it — asgi calls this at module scope on purpose, because that is the
moment gunicorn is about to serve, and a module that raises on import is exactly
the stop wanted there.

Why a stop and not a warning: on the published `SECRET_KEY`, Django signs the
session cookie with a value anyone can read from this repository, and the
dashboard's second-factor flag is a key inside that signed session. A forged
cookie is therefore a complete sign-in with both factors, and every health probe
stays green while it happens. `CUSTOMER_SALT` is the HMAC key that pseudonymises
customer references; its old fallback was the tenant id, stored on every row it
protects.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured


def refuse_development_secrets() -> None:
    """Raise if a production process is about to run on a published default."""
    from django.conf import settings

    if settings.DEBUG:
        # A laptop, where not having to set anything is the point.
        return

    insecure = []
    if settings.SECRET_KEY == settings.INSECURE_SECRET_KEY:
        insecure.append("COMPLYLAYER_SECRET_KEY")
    if settings.COMPLYLAYER["CUSTOMER_SALT"] == settings.INSECURE_CUSTOMER_SALT:
        insecure.append("COMPLYLAYER_CUSTOMER_SALT")

    if not insecure:
        return

    raise ImproperlyConfigured(
        f"refusing to start: {', '.join(insecure)} still holds the development value published "
        "in this repository. Generate each with "
        "`python -c 'import secrets; print(secrets.token_urlsafe(64))'` and set it in the "
        "environment. Note that COMPLYLAYER_CUSTOMER_SALT cannot be rotated freely: changing it "
        "re-pseudonymises every future decision, so history stops joining to the new value. "
        "Run `manage.py complylayer_doctor` for the rest of the preflight."
    )
