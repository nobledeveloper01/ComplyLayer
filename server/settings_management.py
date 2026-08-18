"""Settings for the management workload.

The same image as the decision workload, chosen by DJANGO_SETTINGS_MODULE. This
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

# `Secure` was the one flag missing from that list, which made the omission read
# as a decision rather than an oversight. It is not: this session carries the
# completed second factor, so a single request over plain HTTP hands an attacker
# on the network both factors at once. Off under DEBUG only, because a laptop
# serves http and a cookie the browser refuses to send is a dashboard nobody can
# sign into.
SESSION_COOKIE_SECURE = not DEBUG  # noqa: F405
CSRF_COOKIE_SECURE = not DEBUG  # noqa: F405

# A year, and only meaningful once the first response arrives over HTTPS. Kept
# out of `SECURE_SSL_REDIRECT` territory deliberately: TLS terminates at the load
# balancer in every deployment this ships with, and an app-level redirect behind
# a terminating proxy is a loop unless the header below is trusted first.
SECURE_HSTS_SECONDS = 0 if DEBUG else 31_536_000  # noqa: F405
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG  # noqa: F405
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

MIDDLEWARE = [
    # The decision middleware is dropped here: a management worker has no
    # decision endpoint to serve, and warming a decision rule cache on it would
    # spend memory on something it never reads (D7).
    *[m for m in MIDDLEWARE if "decision_middleware" not in m],  # noqa: F405
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # Approve is a single click that changes what money is allowed to do. Framed
    # inside an attacker's page, one misdirected click on a signed-in officer's
    # browser weakens a control — and the audit trail records it as that
    # officer's own decision, correctly, which is what makes it hard to unpick
    # afterwards. `manage.py check --deploy` found this absent.
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "complylayer.api.middleware.ApiKeyMiddleware",
]

X_FRAME_OPTIONS = "DENY"

# Silenced with reasons, so that `check --deploy` can be a blocking gate rather
# than a wall of warnings nobody reads.
SILENCED_SYSTEM_CHECKS = [
    # SECURE_SSL_REDIRECT. TLS terminates at the load balancer in every
    # deployment this ships with, and an app-level redirect behind a terminating
    # proxy is a redirect loop. SECURE_PROXY_SSL_HEADER above is how Django is
    # told the connection was secure.
    "security.W008",
    # SECURE_HSTS_PRELOAD. Submitting to the browser preload list is a one-way
    # door for a customer's domain, not ours to set on their behalf.
    "security.W021",
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
