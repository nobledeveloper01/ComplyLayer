"""Settings for the management workload.

The same image as the decision workload, chosen by COMPLYLAYER_ROLE. This module
is the whole of D7's separation: it mounts the management URLconf, which the
decision settings never do — so a decision worker has no route to rule
management, and a heavy backtest cannot be scheduled onto a pod serving the
critical path.
"""

from server.settings import *  # noqa: F403

ROOT_URLCONF = "server.urls_management"

INSTALLED_APPS = [*INSTALLED_APPS, "rest_framework"]  # noqa: F405

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

# Sessions are short and rotate on privilege change (§8.2). A dashboard session
# that outlives the working day is a session somebody else can find on a shared
# machine.
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

MIDDLEWARE = [
    # The decision middleware is dropped here: a management worker has no
    # decision endpoint to serve, and warming a decision rule cache on it would
    # spend memory on something it never reads (D7).
    *[m for m in MIDDLEWARE if "decision_middleware" not in m],  # noqa: F405
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "complylayer.api.middleware.ApiKeyMiddleware",
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["complylayer.api.management.permissions.IsAuthenticatedKey"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # Page numbers rather than cursors. Cursor pagination needs a per-view
    # ordering and gives a stable page under concurrent writes; it is the better
    # answer for the decision log and arrives with the analytics work in phase 7,
    # where there is enough volume for the difference to matter.
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "UNAUTHENTICATED_USER": None,
}
