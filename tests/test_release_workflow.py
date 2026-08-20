"""Nothing gets published except from a tag.

`release.yml` can be dispatched by hand so the pipeline can be rehearsed — its
actions only ever ran on a tag, so three of them were bumped across major
versions with nothing exercising them until the next release. A pipeline nobody
can rehearse is one that gets tested by needing it.

The hazard that introduces is obvious and irreversible: a rehearsal that
publishes. PyPI and npm do not meaningfully un-publish, and a signed image in
ghcr is a thing customers pull. So the rule is structural — every step that
uploads, signs, or exchanges a registry credential is gated on
`github.event_name == 'push'`, and a dispatch has no tag to push.

This file is that rule, enforced. The comment in the workflow explains the
intent; this fails the build when somebody adds a publishing step and forgets
the condition, which is the direction the mistake actually gets made.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

WORKFLOW = pathlib.Path(__file__).parent.parent / ".github" / "workflows" / "release.yml"
PUSH_ONLY = "github.event_name == 'push'"

# Steps that upload, sign, or exchange a credential. Matched on substrings
# because the exact action versions move and the hazard does not.
PUBLISHING = ("pypi-publish", "npm publish", "cosign sign", "login-action")

# `npm publish --dry-run` packs a tarball and prints it without contacting the
# registry, which is the point of having it in the rehearsal.
SAFE = ("--dry-run",)


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def steps(workflow):
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            yield job_name, step, f"{step.get('uses', '')} {step.get('run', '')}"


def test_the_workflow_can_be_dispatched(workflow):
    """Without this, the release actions are only ever exercised by a release."""
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "workflow_dispatch" in triggers
    assert "push" in triggers, "a tag must still be what releases"


def test_a_dispatch_takes_no_publish_flag(workflow):
    """A checkbox that turns a rehearsal into an irreversible upload is a
    footgun. A dispatch is always a dry run, with nothing to get wrong."""
    triggers = workflow[True] if True in workflow else workflow["on"]
    dispatch = triggers.get("workflow_dispatch") or {}
    assert not dispatch.get("inputs"), (
        f"workflow_dispatch declares inputs {list(dispatch.get('inputs', {}))}. "
        "A dispatch is unconditionally a dry run; an input is a way to publish by accident."
    )


def test_every_publishing_step_is_gated_on_a_tag_push(workflow):
    ungated = []
    for job_name, step, text in steps(workflow):
        if not any(k in text for k in PUBLISHING):
            continue
        if any(s in text for s in SAFE):
            continue
        if PUSH_ONLY not in str(step.get("if", "")):
            name = step.get("name") or step.get("uses") or text
            ungated.append(f"{job_name}: {name.splitlines()[0][:60]}")

    assert not ungated, (
        f"these steps publish, sign or authenticate without being gated on a tag push: "
        f"{ungated}. Add `if: {PUSH_ONLY}` — a dispatch is a rehearsal and must not "
        "upload anything."
    )


def test_the_image_is_not_pushed_on_a_dispatch(workflow):
    build = next(step for _, step, text in steps(workflow) if "build-push-action" in text)
    with_ = build["with"]
    assert PUSH_ONLY in str(with_["push"]), "the image would be pushed from a rehearsal"
    assert "!=" in str(with_["load"]), "a dry run must load locally so trivy has something to scan"


def test_the_image_is_scanned_either_way(workflow):
    """The rehearsal is worth less if it skips the check that blocks releases."""
    trivy = next(step for _, step, text in steps(workflow) if "trivy-action" in text)
    assert "if" not in trivy, "trivy should run on a dispatch too"
    assert trivy["with"]["exit-code"] == "1", "a scan that cannot fail is not a gate"


def test_the_release_credentials_are_not_requested_by_a_rehearsal(workflow):
    """`environment: release` holds the publishing secrets. A run that does not
    publish has no business requesting them, or waiting on their approval."""
    for name in ("python", "node"):
        env = str(workflow["jobs"][name].get("environment", ""))
        assert PUSH_ONLY in env, f"job {name} requests its environment unconditionally: {env!r}"


def test_every_action_is_still_pinned_to_a_commit(workflow):
    """The property five Dependabot bumps had to preserve."""
    import re

    unpinned = [
        text.strip()
        for _, step, text in steps(workflow)
        if step.get("uses") and not re.search(r"@[0-9a-f]{40}\b", step["uses"])
    ]
    assert not unpinned, f"these actions are not pinned to a commit: {unpinned}"
