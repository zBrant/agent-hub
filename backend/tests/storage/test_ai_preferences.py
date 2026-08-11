"""The authored AI preference is one durable, replaceable row."""

from app.storage.repository import Repository


async def test_ai_preference_upsert_replaces_the_singleton(repo: Repository) -> None:
    assert await repo.get_ai_preference() is None

    first = await repo.upsert_ai_preference(
        planner_backend="harness",
        planner_harness="claude-code",
        planner_model=None,
        search_backend="harness",
        search_harness="codex",
        search_model=None,
        planner_effort="high",
        at_ms=1_000,
    )
    assert first.id == 1
    assert first.updated_ms == 1_000

    second = await repo.upsert_ai_preference(
        planner_backend="api",
        planner_harness=None,
        planner_model="claude-opus-5",
        search_backend="api",
        search_harness=None,
        search_model="claude-sonnet-5",
        planner_effort="max",
        at_ms=2_000,
    )

    assert second.id == 1
    assert second.planner_backend == "api"
    assert second.search_model == "claude-sonnet-5"
    assert second.planner_effort == "max"
    assert second.updated_ms == 2_000
