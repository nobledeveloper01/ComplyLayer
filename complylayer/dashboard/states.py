"""The six states the plan review found missing.

The roadmap originally listed views. Views are the easy part; states are where
compliance software gets uncomfortable, and a state nobody designed becomes an
empty table with a shrug in it.

Each one below is a designed screen with a stated reason for existing.
"""

from __future__ import annotations

from dataclasses import dataclass

# The threshold §11.4 alerts on. The queue must be at its best precisely when it
# is at its worst.
QUEUE_PRESSURE_THRESHOLD = 500


@dataclass(frozen=True)
class State:
    key: str
    title: str
    detail: str
    tone: str = "neutral"  # neutral | reassuring | pressure


def pending_approval_seen_by_author(author: str) -> State:
    """Read-only for the person who wrote it.

    Editing is allowed and clears the approval — but the screen has to say so
    before somebody discovers it by losing an approval they waited two days for.
    """
    return State(
        key="pending_own",
        title="Waiting for someone else to approve this",
        detail=(
            "You wrote this rule, so you cannot approve it yourself. "
            "Editing it now will clear any approval it has already received, "
            "because an approval that survives an edit is not an approval of "
            "anything in particular."
        ),
    )


def backtest_running(processed: int, total: int) -> State:
    """A long job over 30 days of decisions, with somewhere to go.

    Percentage rather than a spinner, and an explicit statement that leaving is
    safe, because the alternative is a compliance officer sitting on a page for
    four minutes in case navigating away cancels it.
    """
    percent = int(processed / total * 100) if total else 0
    return State(
        key="backtest_running",
        title=f"Testing against history — {percent}%",
        detail=(
            f"Checked {processed:,} of {total:,} transactions. "
            "This runs on a copy of the data and cannot affect live decisions. "
            "You can leave this page; the result will be here when you come back."
        ),
    )


def shadow_no_divergence(days: int, decisions: int) -> State:
    """The most common shadow state, and the one that says the rule is safe.

    It should be the most reassuring screen in the product rather than an empty
    table — this is the moment an officer decides whether to trust a control.
    """
    return State(
        key="shadow_agrees",
        title="This rule has agreed with the live rules every time",
        detail=(
            f"Over {days} days and {decisions:,} decisions, this rule would not have "
            "changed a single outcome. That is what you want to see before "
            "activating it."
        ),
        tone="reassuring",
    )


def review_queue_empty() -> State:
    """The goal state, not an error state."""
    return State(
        key="queue_empty",
        title="Nothing waiting for review",
        detail="Every flagged transaction has been looked at.",
        tone="reassuring",
    )


def review_queue_under_pressure(depth: int) -> State:
    """Past the depth §11.4 alerts on: flags are outpacing reviewers.

    The screen changes rather than just getting longer, because a queue of 500
    is not a queue of 20 with more rows — it is a different problem, and the
    useful action is finding the rule that is over-firing.
    """
    return State(
        key="queue_pressure",
        title=f"{depth:,} transactions waiting",
        detail=(
            "Flags are arriving faster than they are being reviewed. This is "
            "usually one rule firing too broadly rather than a genuine rise in "
            "risk — the rule analytics page ranks rules by how often they fire "
            "and how often a review clears them."
        ),
        tone="pressure",
    )


def degraded_banner(count: int, since: str) -> State:
    """Shown on every screen displaying decision data while a fallback is in use.

    Not a toast. A reviewer reading a decision log during a degraded window is
    reading incomplete data, and has to know that while they read it.
    """
    return State(
        key="degraded",
        title=f"{count:,} decisions since {since} were made with a control that did not run",
        detail=(
            "A fallback was applied. Blocking rules failed closed and flagging "
            "rules failed open. These decisions are marked and will be added to "
            "the review queue once service returns, so the record has no gap."
        ),
        tone="pressure",
    )


ALL_STATES = (
    "pending_own",
    "backtest_running",
    "shadow_agrees",
    "queue_empty",
    "queue_pressure",
    "degraded",
)
