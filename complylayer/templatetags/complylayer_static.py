"""`{% static_v %}` — a static URL a browser cannot serve a stale copy of.

Editing `dashboard.css` and reloading showed the old stylesheet: the markup was
new, the rules were not, and the page rendered as an unstyled form. That is a
confusing five minutes every time the CSS changes, and it is worse than
confusing on a deploy — a released dashboard whose stylesheet is a version
behind is a dashboard nobody can trust the look of.

The stamp differs by environment because the two situations want opposite
things:

- **Under DEBUG**, the file's mtime. It changes the moment the file is saved, so
  a reload after an edit is always the edited file. Statting a handful of assets
  per render is irrelevant next to `runserver` itself.
- **Otherwise**, the package version. It is stable for the life of a release —
  so the asset stays cacheable, which is the entire point of a far-future
  `Cache-Control` — and changes exactly when a deploy changes the files.

This is deliberately not `ManifestStaticFilesStorage`. That hashes contents and
rewrites filenames at `collectstatic`, which is the better answer for a large
asset pipeline and overkill for two stylesheets and three fonts — and it needs a
build step that the demo script, which runs `runserver` against a throwaway
database, does not have.
"""

from __future__ import annotations

import functools
from pathlib import Path

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@functools.cache
def _release_stamp() -> str:
    from complylayer import __version__

    return __version__


def _mtime_stamp(path: str) -> str | None:
    """The mtime of the file behind `path`, or None if it cannot be found.

    Not cached: under DEBUG the whole point is to notice an edit.
    """
    found = finders.find(path)
    if not found:
        return None
    try:
        return str(int(Path(found).stat().st_mtime))
    except OSError:
        return None


@register.simple_tag
def static_v(path: str) -> str:
    """`{% static %}` with a version query appended.

    Falls back to a plain static URL if the file cannot be located, so a typo in
    a template name breaks the same way it always did rather than in a new one.
    """
    url = static(path)
    stamp = _mtime_stamp(path) if settings.DEBUG else _release_stamp()
    return f"{url}?v={stamp}" if stamp else url
