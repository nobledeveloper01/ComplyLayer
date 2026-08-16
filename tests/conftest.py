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
    settings.MIDDLEWARE = [*settings.MIDDLEWARE, *EXTRA_MIDDLEWARE]
    settings.TEMPLATES = TEMPLATES
    settings.REST_FRAMEWORK = REST_FRAMEWORK

    auth.clear_cache()
    yield settings
    auth.clear_cache()
