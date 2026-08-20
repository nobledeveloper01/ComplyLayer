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


def trivy_steps(workflow):
    return [step for _, step, text in steps(workflow) if "trivy-action" in text]


def test_the_image_is_scanned_either_way(workflow):
    """The rehearsal is worth less if it skips the check that blocks releases."""
    scans = trivy_steps(workflow)
    assert scans, "the image is not scanned at all"
    for scan in scans:
        assert "if" not in scan, "trivy should run on a dispatch too"


def test_one_scan_can_still_fail_the_release(workflow):
    """The gate ignores CVEs with no available fix — the first dry run found 34
    of them, every one with an empty Fixed Version — but it must still block on
    anything that *can* be acted on. A scan where nothing fails is decoration."""
    blocking = [s for s in trivy_steps(workflow) if s["with"]["exit-code"] == "1"]
    assert len(blocking) == 1, f"expected exactly one blocking scan, found {len(blocking)}"
    assert blocking[0]["with"]["ignore-unfixed"] is True, (
        "the blocking scan must ignore unfixed CVEs, or it fails on findings nobody "
        "can act on and gets deleted within a month"
    )


def test_the_unfixed_ones_are_still_reported(workflow):
    """Ignoring a CVE in the gate says it cannot be acted on today, not that it
    does not exist. Somebody reading a release log should still see it."""
    reporting = [s for s in trivy_steps(workflow) if s["with"]["exit-code"] == "0"]
    assert len(reporting) == 1, "nothing reports the CVEs the gate ignores"
    assert reporting[0]["with"]["ignore-unfixed"] is False


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


# Registry publishing is opt-in per registry. PyPI needs a trusted publisher
# configured there and npm needs an organisation and a token, and neither is
# something the repository can arrange for itself. Before this, a tag failed
# both jobs on credentials nobody had set up — a red release for a reason no
# reader could act on, which is how a gate stops being read at all.
REGISTRY_FLAGS = {
    "pypi-publish": "vars.PUBLISH_PYPI == 'true'",
    "npm publish": "vars.PUBLISH_NPM == 'true'",
}


def test_each_registry_upload_is_also_gated_on_its_own_flag(workflow):
    """A tag releases the image. It uploads to a registry only if that registry
    has been wired up, and the flag is the record of whether it has."""
    for marker, flag in REGISTRY_FLAGS.items():
        uploads = [
            step
            for _, step, text in steps(workflow)
            if marker in text and not any(s in text for s in SAFE)
        ]
        assert uploads, f"no upload step found for {marker}"
        for step in uploads:
            condition = str(step.get("if", ""))
            assert flag in condition, (
                f"the {marker} step is not gated on {flag}: {condition!r}. Without it a "
                "tag fails on a credential nobody configured, rather than releasing what it can."
            )


def test_the_flags_cannot_publish_without_a_tag(workflow):
    """The registry flag is an extra condition, never a replacement for the tag
    gate — a repository variable must not be able to turn a rehearsal into an
    upload. This is `test_every_publishing_step_is_gated_on_a_tag_push` stated
    for the specific way that gate could now be lost."""
    for marker in REGISTRY_FLAGS:
        for _, step, text in steps(workflow):
            if marker not in text or any(s in text for s in SAFE):
                continue
            assert PUSH_ONLY in str(step.get("if", "")), (
                f"the {marker} step lost its tag gate when the registry flag was added"
            )


def test_the_image_does_not_depend_on_a_registry_flag(workflow):
    """ghcr needs no credential this repository lacks — the token is automatic
    and cosign signs keylessly. The image is what a tag always releases, so
    gating it behind an opt-in would make a tag able to release nothing at all."""
    for _, step, text in steps(workflow):
        if "cosign sign" in text or "login-action" in text:
            condition = str(step.get("if", ""))
            assert "vars.PUBLISH" not in condition, (
                f"the image step is gated on a registry flag ({condition!r}); a tag must always "
                "produce a signed image"
            )
