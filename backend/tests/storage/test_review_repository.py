"""The human gate's two tables: what a reviewer decided about one attempt.

`docs/phase-2.md` C7 and `design.md` §9. Nothing here evaluates a criterion —
§9 settled that a human does that — so every test is about the *record*: that
it is per attempt, that it is a snapshot rather than a live join, and above all
that replay cannot reach it.

That last one is the reason the rows are keyed by ``(node_id, attempt)`` instead
of by ``run_id``, and it gets a test with real replay behind it rather than an
argument in a docstring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.harnesses.events import AgentEvent
from app.models.pricing import PriceHistory, PriceTable
from app.models.tables import CriterionOutcome, Node, ReviewDecision, Run
from app.storage.ingest import ingest_run
from app.storage.meta import RunMeta
from app.storage.replay import replay_run
from app.storage.repository import Repository, RepositoryError

AT_MS = 1_700_000_000_000

CRITERIA = (
    "pytest tests/test_auth.py passes",
    "the OpenAPI schema is regenerated",
    "no new TODO comments",
)


async def snapshot(repo: Repository, node: Node, *, attempt: int = 1) -> None:
    await repo.record_acceptance_criteria(
        node_id=node.id, attempt=attempt, criteria=CRITERIA, at_ms=AT_MS
    )


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------


async def test_recording_criteria_stores_one_unevaluated_row_each(
    repo: Repository, node_row: Node
) -> None:
    """`design.md` §9: the run records, it does not judge."""
    written = await repo.record_acceptance_criteria(
        node_id=node_row.id, attempt=1, criteria=CRITERIA, at_ms=AT_MS
    )

    assert [row.position for row in written] == [0, 1, 2]
    stored = await repo.list_acceptance_results(node_row.id)
    assert [(row.attempt, row.criterion, row.outcome) for row in stored] == [
        (1, CRITERIA[0], CriterionOutcome.UNEVALUATED),
        (1, CRITERIA[1], CriterionOutcome.UNEVALUATED),
        (1, CRITERIA[2], CriterionOutcome.UNEVALUATED),
    ]
    assert [row.created_ms for row in stored] == [AT_MS] * 3
    assert [row.updated_ms for row in stored] == [AT_MS] * 3


async def test_a_node_with_no_criteria_records_nothing(
    repo: Repository, node_row: Node
) -> None:
    """Empty is a real answer, not a missing one, and it costs no rows."""
    assert (
        await repo.record_acceptance_criteria(
            node_id=node_row.id, attempt=1, criteria=(), at_ms=AT_MS
        )
        == []
    )
    assert await repo.list_acceptance_results(node_row.id) == []


async def test_recording_the_same_attempt_twice_is_refused_by_the_key(
    repo: Repository, node_row: Node
) -> None:
    """Finalizing one run twice is a bug, and a bug must be loud (§9).

    The alternative — replacing the rows — would silently discard whatever the
    reviewer had already written on them.
    """
    await snapshot(repo, node_row)

    with pytest.raises(IntegrityError, match="acceptance_result"):
        await snapshot(repo, node_row)


async def test_the_snapshot_does_not_move_when_the_node_is_edited(
    repo: Repository, node_row: Node
) -> None:
    """The text is copied, not joined.

    A node's ``acceptance_criteria`` are authored input a human may rewrite at
    any time. If the results were a live join through ``position``, editing the
    list after a review would silently re-label a verdict as being about a
    criterion nobody judged.
    """
    await snapshot(repo, node_row)
    node_row.acceptance_criteria = ("something else entirely",)
    await repo.session.commit()

    assert [
        row.criterion for row in await repo.list_acceptance_results(node_row.id)
    ] == (list(CRITERIA))


# ---------------------------------------------------------------------------
# Resolving them
# ---------------------------------------------------------------------------


async def test_a_reviewer_can_pass_two_and_fail_one(
    repo: Repository, node_row: Node
) -> None:
    """`docs/phase-2.md` C7: each criterion carries its own outcome."""
    await snapshot(repo, node_row)

    await repo.resolve_acceptance_results(
        node_id=node_row.id,
        attempt=1,
        outcomes={
            0: CriterionOutcome.PASS,
            1: CriterionOutcome.FAIL,
            2: CriterionOutcome.PASS,
        },
        at_ms=AT_MS + 5_000,
    )

    stored = await repo.list_acceptance_results(node_row.id)
    assert [row.outcome for row in stored] == [
        CriterionOutcome.PASS,
        CriterionOutcome.FAIL,
        CriterionOutcome.PASS,
    ]
    assert [row.updated_ms for row in stored] == [AT_MS + 5_000] * 3
    # ...and the run's own record of when it took the snapshot did not move.
    assert [row.created_ms for row in stored] == [AT_MS] * 3


async def test_resolving_part_of_the_checklist_leaves_the_rest_unevaluated(
    repo: Repository, node_row: Node
) -> None:
    """A reviewer who only checked one thing said only one thing."""
    await snapshot(repo, node_row)

    await repo.resolve_acceptance_results(
        node_id=node_row.id, attempt=1, outcomes={1: CriterionOutcome.FAIL}
    )

    assert [row.outcome for row in await repo.list_acceptance_results(node_row.id)] == [
        CriterionOutcome.UNEVALUATED,
        CriterionOutcome.FAIL,
        CriterionOutcome.UNEVALUATED,
    ]


async def test_judging_a_criterion_the_attempt_never_had_is_an_error(
    repo: Repository, node_row: Node
) -> None:
    await snapshot(repo, node_row)

    with pytest.raises(RepositoryError, match="position 7"):
        await repo.resolve_acceptance_results(
            node_id=node_row.id, attempt=1, outcomes={7: CriterionOutcome.PASS}
        )


async def test_each_attempt_is_judged_separately(
    repo: Repository, node_row: Node
) -> None:
    """A retry is a different diff (B7), so it is a different verdict.

    Carrying attempt 1's ``pass`` forward would be a claim about code that no
    longer exists.
    """
    await snapshot(repo, node_row, attempt=1)
    await repo.resolve_acceptance_results(
        node_id=node_row.id, attempt=1, outcomes={0: CriterionOutcome.FAIL}
    )
    await snapshot(repo, node_row, attempt=2)

    assert [
        (row.attempt, row.position, row.outcome)
        for row in await repo.list_acceptance_results(node_row.id)
    ] == [
        (1, 0, CriterionOutcome.FAIL),
        (1, 1, CriterionOutcome.UNEVALUATED),
        (1, 2, CriterionOutcome.UNEVALUATED),
        (2, 0, CriterionOutcome.UNEVALUATED),
        (2, 1, CriterionOutcome.UNEVALUATED),
        (2, 2, CriterionOutcome.UNEVALUATED),
    ]
    assert [
        row.position
        for row in await repo.list_acceptance_results(node_row.id, attempt=2)
    ] == ([0, 1, 2])


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


async def test_reviews_are_returned_in_attempt_order(
    repo: Repository, node_row: Node
) -> None:
    """The order the rejections are replayed into the next prompt."""
    await repo.record_review(
        node_id=node_row.id,
        attempt=2,
        decision=ReviewDecision.REJECTED,
        feedback="second",
        at_ms=AT_MS + 1,
    )
    await repo.record_review(
        node_id=node_row.id,
        attempt=1,
        decision=ReviewDecision.REJECTED,
        feedback="first",
        at_ms=AT_MS,
    )

    assert [
        (row.attempt, row.feedback) for row in await repo.list_reviews(node_row.id)
    ] == [(1, "first"), (2, "second")]


async def test_an_approval_carries_no_feedback_and_says_so(
    repo: Repository, node_row: Node
) -> None:
    row = await repo.record_review(
        node_id=node_row.id, attempt=1, decision=ReviewDecision.APPROVED, at_ms=AT_MS
    )

    assert row.decision is ReviewDecision.APPROVED
    # None, not "": an empty string would claim the reviewer typed something.
    assert row.feedback is None


async def test_re_reviewing_one_attempt_replaces_that_attempts_verdict(
    repo: Repository, node_row: Node
) -> None:
    """One row per attempt. The newer answer is the one that counts."""
    await repo.record_review(
        node_id=node_row.id,
        attempt=1,
        decision=ReviewDecision.REJECTED,
        feedback="wrong",
        at_ms=AT_MS,
    )
    await repo.record_review(
        node_id=node_row.id,
        attempt=1,
        decision=ReviewDecision.APPROVED,
        at_ms=AT_MS + 10,
    )

    reviews = await repo.list_reviews(node_row.id)
    assert len(reviews) == 1
    assert reviews[0].decision is ReviewDecision.APPROVED
    assert reviews[0].feedback is None


async def test_a_review_of_a_node_that_does_not_exist_is_refused(
    repo: Repository,
) -> None:
    with pytest.raises(RepositoryError, match="no such node"):
        await repo.record_review(
            node_id="node_missing", attempt=1, decision=ReviewDecision.APPROVED
        )


async def test_deleting_a_node_takes_its_reviews_with_it(
    repo: Repository, node_row: Node
) -> None:
    """Editing a proposal is the only thing that deletes a node.

    The verdicts were about that node's diff and have no meaning without it, so
    the foreign key cascades rather than leaving them behind.
    """
    await snapshot(repo, node_row)
    await repo.record_review(
        node_id=node_row.id, attempt=1, decision=ReviewDecision.REJECTED, feedback="no"
    )

    assert await repo.delete_node(node_row.id) is True

    assert await repo.list_acceptance_results(node_row.id) == []
    assert await repo.list_reviews(node_row.id) == []


# ---------------------------------------------------------------------------
# Invariant 4: replay may not reach an authored verdict
# ---------------------------------------------------------------------------


async def test_replaying_a_run_leaves_the_reviewers_verdict_untouched(
    repo: Repository,
    runs_root: Path,
    logged_run: Run,
    run_meta: RunMeta,
    prices: PriceTable,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    """The whole reason the key is ``(node_id, attempt)`` and not ``run_id``.

    ``replay_run`` deletes the ``run`` row and rebuilds it from the log. A
    foreign key onto ``run`` would have taken the human's decision with it
    through ``ON DELETE CASCADE`` — invariant 4 permits replay to discard
    *derived* rows and only those, and a reviewer's verdict appears in no
    ``events.ndjson`` anywhere.

    Break the key back to ``run_id`` and this test is the one that goes red.
    """
    async with ingest_run(
        repository=repo, runs_root=runs_root, meta=run_meta, prices=prices
    ) as ingest:
        for event in event_stream:
            await ingest.ingest(event)
    await repo.record_acceptance_criteria(
        node_id=logged_run.node_id,
        attempt=logged_run.attempt,
        criteria=CRITERIA,
        at_ms=AT_MS,
    )
    await repo.resolve_acceptance_results(
        node_id=logged_run.node_id,
        attempt=logged_run.attempt,
        outcomes={0: CriterionOutcome.PASS, 1: CriterionOutcome.FAIL},
        at_ms=AT_MS,
    )
    await repo.record_review(
        node_id=logged_run.node_id,
        attempt=logged_run.attempt,
        decision=ReviewDecision.REJECTED,
        feedback="the schema was not regenerated",
        at_ms=AT_MS,
    )

    await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=logged_run.id,
        prices=price_history,
    )

    assert [
        row.outcome for row in await repo.list_acceptance_results(logged_run.node_id)
    ] == [CriterionOutcome.PASS, CriterionOutcome.FAIL, CriterionOutcome.UNEVALUATED]
    reviews = await repo.list_reviews(logged_run.node_id)
    assert [(row.attempt, row.feedback) for row in reviews] == [
        (logged_run.attempt, "the schema was not regenerated")
    ]
    # ...and the rebuilt run really did replace the row, rather than the test
    # having proved nothing because replay quietly did not run.
    rebuilt = await repo.get_run(logged_run.id)
    assert rebuilt is not None and rebuilt.event_count == len(event_stream)
