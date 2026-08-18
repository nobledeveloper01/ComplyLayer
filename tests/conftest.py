"""Shared fixtures.

The management settings live here in one place because two suites need them and
two definitions drift. They did: the isolation suite and the dashboard suite each
declared their own `REST_FRAMEWORK`, and under random test ordering one run left
pagination configured and the next did not — so a test asserting on
`response.json()["results"]` passed alone and failed in a full run.

A fixture that is copied is a fixture that disagrees with itself eventually.
"""

from __future__ import annotations

import pytest

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["complylayer.api.management.permissions.IsAuthenticatedKey"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "DIRS": [],
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            ]
        },
    }
]

EXTRA_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "complylayer.api.middleware.ApiKeyMiddleware",
]


@pytest.fixture
def management(settings):
    """The management workload's settings, as `server.settings_management` has them."""
    from complylayer.api import auth

    settings.ROOT_URLCONF = "server.urls_management"
    settings.INSTALLED_APPS = [*settings.INSTALLED_APPS, "rest_framework"]
    # Strip the decision middleware, exactly as `server/settings_management.py`
    # does. Leaving it in did two wrong things at once: authentication answered
    # 401 before DRF could answer 403, and every management test started a
    # version-watcher thread that then polled a database the test was about to
    # roll back — surfacing as unrelated failures in whichever test happened to
    # be running when the thread raised.
    settings.MIDDLEWARE = [
        *[m for m in settings.MIDDLEWARE if "decision_middleware" not in m],
        *EXTRA_MIDDLEWARE,
    ]
    settings.TEMPLATES = TEMPLATES
    settings.REST_FRAMEWORK = REST_FRAMEWORK

    auth.clear_cache()
    yield settings
    auth.clear_cache()


@pytest.fixture
def view_only(settings):
    """Settings without the decision middleware.

    For suites that test a *view* by attaching its dependencies themselves. The
    middleware now owns `decision_handler` and `ruleset_cache` and correctly
    overwrites anything a test put there — which is the whole point of it
    existing, and the reason those attributes were missing in production for
    eight phases.

    The integrated path is covered by `tests/test_decision_wiring.py`, which
    attaches nothing.
    """
    settings.MIDDLEWARE = [m for m in settings.MIDDLEWARE if "decision_middleware" not in m]
    return settings
