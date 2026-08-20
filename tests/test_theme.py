"""Light is the default, and the reader can change it.

The dashboard followed `prefers-color-scheme` whenever no cookie was set, so
anybody whose machine preferred dark met the product in dark — and the warm
paper palette the whole design is built around was the one nobody saw. That is
not a bug a test suite would ever have caught, because every assertion here is
about behaviour and none of them opened a browser.

These are cheap structural checks over the stylesheet and the template. They do
not prove the dashboard looks right; they prove the two specific things that
were wrong cannot come back silently.
"""

from __future__ import annotations

import pathlib
import re

import pytest

STATIC = pathlib.Path(__file__).parent.parent / "complylayer" / "static" / "complylayer"
TEMPLATES = pathlib.Path(__file__).parent.parent / "complylayer" / "templates" / "complylayer"


@pytest.fixture(scope="module")
def tokens() -> str:
    return (STATIC / "tokens.css").read_text()


@pytest.fixture(scope="module")
def base() -> str:
    return (TEMPLATES / "base.html").read_text()


def test_dark_is_never_applied_without_an_explicit_choice(tokens):
    """The regression itself. The dark media query must be gated on the reader
    having asked to follow the OS, not on merely not having asked for light."""
    block = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*(:root[^\{]*)\{", tokens)
    assert block, "no prefers-color-scheme block found"
    selector = block.group(1).strip()
    assert selector == '[data-theme="system"]' or selector == ':root[data-theme="system"]', (
        f"the dark media query applies to {selector!r}. Anything broader means a first "
        "visit with no cookie can render dark, which is how the light palette went unseen."
    )


def test_the_light_palette_is_the_unconditional_default(tokens):
    """`:root` — no media query, no attribute — must carry the paper surfaces."""
    root = tokens.split(":root {", 1)[1].split("}", 1)[0]
    assert "--surface: #f7f5f1" in root.lower(), "the bare :root is not the light palette"


def test_all_three_choices_are_offered(base):
    for choice in ("light", "dark", "system"):
        assert f'data-set-theme="{choice}"' in base, f"no control for {choice}"


def test_the_control_says_which_one_is_active(base):
    """A three-way control with nothing marked is a control nobody trusts."""
    assert "aria-pressed" in base, "the active theme is not exposed"
    assert "aria-labelledby" in base, "the group has no accessible name"


def test_the_choice_survives_the_next_page_load(base):
    assert "complylayer_theme=" in base, "the choice is not persisted"
    assert "max-age=" in base, "the cookie is a session cookie, so the choice is forgotten"


def test_an_unrecognised_cookie_falls_back_to_light(base):
    """`data-theme` is written straight from a cookie, which anyone can set to
    anything. The control must agree with what the stylesheet actually does."""
    assert "!== 'dark' && current !== 'system'" in base, (
        "the control does not normalise an unexpected cookie value to light"
    )
