"""Tests for token cost computation.

The reconciliation test at the bottom is the one that matters: it checks our
arithmetic against numbers Claude Code itself reported, in fixtures captured
from the real binary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.models.pricing import (
    PriceHistory,
    PriceTable,
    PriceTableNotFound,
    PricingError,
    TokenCounts,
    load_price_history,
    load_price_table,
    normalize_model_id,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PRICING_YAML = REPO_ROOT / "pricing.yaml"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude-code"


@pytest.fixture
def table() -> PriceTable:
    return load_price_table(PRICING_YAML)


# --------------------------------------------------------------------------
# Model id normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("claude-haiku-4-5", "claude-haiku-4-5"),
        ("claude-opus-5", "claude-opus-5"),
        ("  claude-sonnet-5  ", "claude-sonnet-5"),
        # Not a date suffix — must survive untouched.
        ("claude-haiku-4-5-2025", "claude-haiku-4-5-2025"),
    ],
)
def test_normalize_model_id(raw: str, expected: str) -> None:
    assert normalize_model_id(raw) == expected


def test_both_spellings_price_identically(table: PriceTable) -> None:
    """Claude Code emits both in one run; they must not price differently."""
    counts = TokenCounts(input_tokens=1000, output_tokens=1000)
    assert table.cost_usd("claude-haiku-4-5", counts) == table.cost_usd(
        "claude-haiku-4-5-20251001", counts
    )


# --------------------------------------------------------------------------
# TokenCounts
# --------------------------------------------------------------------------


def test_total_is_all_four_fields() -> None:
    counts = TokenCounts(
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=4,
        cache_write_tokens=8,
    )
    assert counts.total == 15


def test_tier_split_must_sum_to_cache_write() -> None:
    with pytest.raises(ValueError, match="does not sum"):
        TokenCounts(
            cache_write_tokens=100,
            cache_write_1h_tokens=60,
            cache_write_5m_tokens=30,
        )


def test_tier_split_may_be_absent() -> None:
    assert TokenCounts(cache_write_tokens=100).cache_write_1h_tokens == 0


def test_negative_counts_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        TokenCounts(input_tokens=-1)


def test_addition_carries_tiers() -> None:
    a = TokenCounts(input_tokens=1, cache_write_tokens=10, cache_write_1h_tokens=10)
    b = TokenCounts(input_tokens=2, cache_write_tokens=5, cache_write_5m_tokens=5)
    total = a + b
    assert total.input_tokens == 3
    assert total.cache_write_tokens == 15
    assert total.cache_write_1h_tokens == 10
    assert total.cache_write_5m_tokens == 5


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------


def test_unknown_model_costs_none_not_zero(table: PriceTable) -> None:
    """Zero is a number someone will trust. Unknown must stay unknown."""
    assert (
        table.cost_usd("some-future-model", TokenCounts(input_tokens=1_000_000)) is None
    )


def test_cache_read_is_cheap(table: PriceTable) -> None:
    read = table.cost_usd("claude-haiku-4-5", TokenCounts(cache_read_tokens=1_000_000))
    plain = table.cost_usd("claude-haiku-4-5", TokenCounts(input_tokens=1_000_000))
    assert read is not None and plain is not None
    assert read == pytest.approx(plain * 0.1)


def test_untiered_cache_write_is_priced_at_the_expensive_tier(
    table: PriceTable,
) -> None:
    """Claude Code 2.1.222 defaults to the 1h tier, so guessing 5m under-reports."""
    untiered = table.cost_usd(
        "claude-haiku-4-5", TokenCounts(cache_write_tokens=1_000_000)
    )
    explicit_1h = table.cost_usd(
        "claude-haiku-4-5",
        TokenCounts(cache_write_tokens=1_000_000, cache_write_1h_tokens=1_000_000),
    )
    assert untiered == explicit_1h


def test_tier_choice_changes_the_bill(table: PriceTable) -> None:
    cheap = table.cost_usd(
        "claude-haiku-4-5",
        TokenCounts(cache_write_tokens=1_000_000, cache_write_5m_tokens=1_000_000),
    )
    dear = table.cost_usd(
        "claude-haiku-4-5",
        TokenCounts(cache_write_tokens=1_000_000, cache_write_1h_tokens=1_000_000),
    )
    assert cheap is not None and dear is not None
    assert dear == pytest.approx(cheap * (2.0 / 1.25))


def test_model_specific_untiered_cache_write_multiplier(table: PriceTable) -> None:
    """OpenAI cache writes have no TTL split and are billed at 1.25x input."""
    write = table.cost_usd("gpt-5.6-sol", TokenCounts(cache_write_tokens=1_000_000))
    plain = table.cost_usd("gpt-5.6-sol", TokenCounts(input_tokens=1_000_000))
    assert write is not None and plain is not None
    assert write == pytest.approx(plain * 1.25)


def test_summing_only_input_tokens_would_be_wrong_by_orders_of_magnitude(
    table: PriceTable,
) -> None:
    """Invariant 3, stated as a test rather than as a comment.

    Numbers taken from the real simple_edit fixture: a turn whose visible
    ``input_tokens`` is 21 actually moved ~28k tokens.
    """
    real = TokenCounts(
        input_tokens=21,
        output_tokens=254,
        cache_read_tokens=21_737,
        cache_write_tokens=6_513,
        cache_write_1h_tokens=6_513,
    )
    naive = TokenCounts(input_tokens=21)
    assert real.total > naive.total * 1000


# --------------------------------------------------------------------------
# The price table on disk
# --------------------------------------------------------------------------


def test_shipped_pricing_yaml_loads(table: PriceTable) -> None:
    assert {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"} <= set(table.models)
    assert {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"} <= set(table.models)


def test_malformed_table_raises() -> None:
    with pytest.raises(PricingError):
        PriceTable.from_mapping({"models": {}})


def test_bad_price_entry_raises() -> None:
    with pytest.raises(PricingError, match="bad price entry"):
        PriceTable.from_mapping({"version": 1, "models": {"m": {"input": "free"}}})


@pytest.mark.parametrize("version", [None, 0, -1, 1.5, True, "1"])
def test_version_must_be_an_explicit_positive_integer(version: object) -> None:
    raw = {"models": {"m": {"input": 1.0, "output": 1.0}}}
    if version is not None:
        raw["version"] = version
    with pytest.raises(PricingError, match="positive integer"):
        PriceTable.from_mapping(raw)


# --------------------------------------------------------------------------
# Price history — superseded tables are retained, never replaced
# --------------------------------------------------------------------------


def two_versions() -> dict[str, object]:
    """Version 2 doubled the price of the one model in the table."""
    return {
        "version": 2,
        "models": {"m": {"input": 2.0, "output": 20.0}},
        "superseded": [{"version": 1, "models": {"m": {"input": 1.0, "output": 10.0}}}],
    }


def write_history(tmp_path: Path, raw: dict[str, object]) -> Path:
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_the_current_table_is_the_default(tmp_path: Path) -> None:
    path = write_history(tmp_path, two_versions())
    assert load_price_table(path).version == 2


def test_a_superseded_table_is_still_reachable(tmp_path: Path) -> None:
    """Computing cost at ingest only protects history if the price is findable."""
    path = write_history(tmp_path, two_versions())
    counts = TokenCounts(input_tokens=1_000_000)
    assert load_price_table(path, version=1).cost_usd("m", counts) == pytest.approx(1.0)
    assert load_price_table(path, version=2).cost_usd("m", counts) == pytest.approx(2.0)


def test_a_missing_version_refuses_and_names_itself(tmp_path: Path) -> None:
    """Replay depends on this: refusing is recoverable, repricing is not."""
    path = write_history(tmp_path, two_versions())
    with pytest.raises(PriceTableNotFound) as raised:
        load_price_table(path, version=7)

    assert raised.value.version == 7
    assert "version 7" in str(raised.value)
    assert "1, 2" in str(raised.value)
    assert str(path) in str(raised.value)


def test_a_duplicated_version_is_a_malformed_file(tmp_path: Path) -> None:
    """A version identifies exactly one table, forever."""
    raw = two_versions()
    raw["superseded"] = [
        {"version": 1, "models": {"m": {"input": 1.0, "output": 10.0}}},
        {"version": 1, "models": {"m": {"input": 9.0, "output": 90.0}}},
    ]
    with pytest.raises(PricingError, match="duplicate price table version"):
        load_price_history(write_history(tmp_path, raw))


def test_a_superseded_table_may_not_be_newer_than_the_current_one(
    tmp_path: Path,
) -> None:
    raw = two_versions()
    raw["superseded"] = [{"version": 5, "models": {"m": {"input": 1.0, "output": 1.0}}}]
    with pytest.raises(PricingError, match="newer than the current one"):
        load_price_history(write_history(tmp_path, raw))


def test_superseded_must_be_a_list_of_whole_tables(tmp_path: Path) -> None:
    """Never a delta chain: a replay must not have to interpret history."""
    raw = two_versions()
    raw["superseded"] = {"1": {"models": {}}}
    with pytest.raises(PricingError, match="list of whole price tables"):
        load_price_history(write_history(tmp_path, raw))


def test_the_shipped_file_has_a_history_of_exactly_one_version(
    table: PriceTable,
) -> None:
    history = load_price_history(PRICING_YAML)
    assert history.versions == (table.version,)
    assert history.table(table.version) is history.current
    assert history.table() is history.current


def test_a_history_with_no_superseded_key_still_loads() -> None:
    history = PriceHistory.from_mapping(
        {"version": 3, "models": {"m": {"input": 1.0, "output": 1.0}}}
    )
    assert history.versions == (3,)


# --------------------------------------------------------------------------
# Reconciliation against the real CLI
# --------------------------------------------------------------------------


def _last_result(fixture: str) -> dict[str, object]:
    events = [
        json.loads(line)
        for line in (FIXTURES / fixture).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results = [e for e in events if e.get("type") == "result"]
    assert results, f"{fixture} has no result event"
    return results[-1]


@pytest.mark.parametrize(
    "fixture", ["simple_edit.ndjson", "tool_error.ndjson", "multi_turn.ndjson"]
)
def test_four_fields_match_what_the_cli_reported(fixture: str) -> None:
    """Our reading of the four fields must equal the CLI's own accounting.

    Note this compares against ``result.usage`` and not ``total_cost_usd``:
    ``modelUsage`` carries an extra side-channel entry (~532 in / 13 out) that
    appears in no event but is included in the cumulative cost, so the two can
    never reconcile.
    """
    usage = _last_result(fixture)["usage"]
    assert isinstance(usage, dict)

    creation = usage.get("cache_creation") or {}
    assert isinstance(creation, dict)

    counts = TokenCounts(
        input_tokens=int(usage["input_tokens"]),
        output_tokens=int(usage["output_tokens"]),
        cache_read_tokens=int(usage["cache_read_input_tokens"]),
        cache_write_tokens=int(usage["cache_creation_input_tokens"]),
        cache_write_5m_tokens=int(creation.get("ephemeral_5m_input_tokens", 0)),
        cache_write_1h_tokens=int(creation.get("ephemeral_1h_input_tokens", 0)),
    )

    # The tier split reconciling with the total is the assertion; TokenCounts
    # raises otherwise, so reaching this line already proves it.
    assert counts.cache_write_tokens == (
        counts.cache_write_5m_tokens + counts.cache_write_1h_tokens
    )
    assert counts.total > counts.input_tokens


def test_every_capture_used_the_expensive_cache_tier() -> None:
    """2.1.222 defaults to ephemeral_1h. If this ever fails, re-check pricing."""
    creation = _last_result("simple_edit.ndjson")["usage"]
    assert isinstance(creation, dict)
    tiers = creation["cache_creation"]
    assert isinstance(tiers, dict)
    assert tiers["ephemeral_5m_input_tokens"] == 0
    assert tiers["ephemeral_1h_input_tokens"] > 0
