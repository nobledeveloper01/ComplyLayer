"""The entrypoint gunicorn loads.

The refusal below runs at import, which is the whole point: this module is
imported by exactly one thing, a server about to take traffic. Settings are
imported by every test and every management command, so the check does not
belong there. `server/boot.py` holds the rule; `complylayer_doctor` reports the
same thing before a deploy and `manage.py check --deploy` reports it in CI.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "server.settings")

from django.core.asgi import get_asgi_application

from server.boot import refuse_development_secrets

application = get_asgi_application()
refuse_development_secrets()
