"""Tests for ``runs/<run_id>/meta.json``.

Two properties carry the weight here: a secret must never reach the file
(`docs/conventions.md` §6), and a run killed before it finished must leave
something readable that does **not** read as trustworthy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.harnesses.base import ParseStats
from app.storage.meta import (
    ENV_ALLOWLIST,
    MetaError,
    ParserTrust,
    RunMeta,
    build_meta,
    meta_path,
    read_meta_sync,
    sanitize_env,
    write_meta_sync,
)

RUN_ID = "run_01J000000000000000000000"
SECRET = "sk-ant-do-not-write-this-down"


def make_meta(**overrides: object) -> RunMeta:
    fields: dict[str, object] = {
        "run_id": RUN_ID,
        "session_id": "sess_01J000000000000000000000",
        "node_id": "node_01J000000000000000000000",
        "attempt": 1,
        "price_table_version": 1,
        "harness": "codex",
        "cwd": Path("/tmp/workspaces/node_a"),
        "created_ms": 1_700_000_000_000,
    }
    fields.update(overrides)
    return build_meta(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Environment sanitizing — a security requirement, not hygiene
# --------------------------------------------------------------------------


def test_allowlist_keeps_launch_conditions_and_nothing_else() -> None:
    kept = sanitize_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/Users/me",
            "ANTHROPIC_API_KEY": SECRET,
            "AWS_SECRET_ACCESS_KEY": SECRET,
            "GITHUB_TOKEN": SECRET,
            "MY_APP_PASSWORD": SECRET,
        }
    )
    assert kept == {"HOME": "/Users/me", "PATH": "/usr/bin"}


def test_a_secret_never_reaches_the_file(tmp_path: Path) -> None:
    """Fed a credential under several plausible names, write none of them."""
    meta = make_meta(
        env={
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": SECRET,
            "OPENAI_API_KEY": SECRET,
            "AWS_SESSION_TOKEN": SECRET,
            "NPM_CONFIG_AUTHTOKEN": SECRET,
        }
    )
    path = tmp_path / "meta.json"
    write_meta_sync(path, meta)

    text = path.read_text(encoding="utf-8")
    assert SECRET not in text
    assert "ANTHROPIC_API_KEY" not in text
    assert json.loads(text)["env"] == {"PATH": "/usr/bin"}


def test_an_unknown_variable_is_dropped_rather_than_matched() -> None:
    """An allowlist, never a denylist: a name nobody predicted still loses."""
    assert sanitize_env({"SOME_FUTURE_VENDOR_CREDENTIAL": SECRET}) == {}


def test_allowlist_holds_no_credential_shaped_names() -> None:
    for name in ENV_ALLOWLIST:
        assert not any(
            hint in name for hint in ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH")
        )


def test_build_meta_is_the_only_way_to_forget_nothing() -> None:
    meta = make_meta(env={"TERM": "xterm-256color", "GITHUB_TOKEN": SECRET})
    assert meta.env == {"TERM": "xterm-256color"}


# --------------------------------------------------------------------------
# Finalization
# --------------------------------------------------------------------------


def test_a_run_killed_before_the_end_is_readable_and_not_finalized(
    tmp_path: Path,
) -> None:
    path = tmp_path / "meta.json"
    write_meta_sync(path, make_meta())

    recovered = read_meta_sync(path)
    assert recovered.finalized is False
    assert recovered.finalized_ms is None
    assert recovered.parser is None
    # The unknown case must not read as the clean case: B7 refuses to merge on
    # this, and a run whose parser never got to report is exactly as unsafe as
    # one that reported a problem.
    assert recovered.trusted is False


def test_finalizing_records_the_parser_verdict(tmp_path: Path) -> None:
    stats = ParseStats(lines=42, events=40)
    stats.count_ignored("token_count")

    meta = make_meta().finalize(at_ms=1_700_000_009_000, stats=stats)
    path = tmp_path / "meta.json"
    write_meta_sync(path, meta)

    recovered = read_meta_sync(path)
    assert recovered.finalized is True
    assert recovered.finalized_ms == 1_700_000_009_000
    assert recovered.parser == ParserTrust(
        lines=42, events=40, ignored={"token_count": 1}
    )
    assert recovered.trusted is True


def test_an_unrecognized_line_makes_the_run_untrusted() -> None:
    stats = ParseStats(lines=10, events=9)
    stats.count_unknown("thread.rollout")
    meta = make_meta().finalize(at_ms=1, stats=stats)
    assert meta.parser is not None
    assert meta.parser.unhandled == 1
    assert meta.trusted is False


def test_unreconciled_usage_makes_the_run_untrusted() -> None:
    """Tokens are missing from the totals, so the cost is understated."""
    stats = ParseStats(lines=10, events=10, zero_usage_turns=2)
    stats.usage_unreconciled_turns = 1
    meta = make_meta().finalize(at_ms=1, stats=stats)
    assert meta.trusted is False


def test_a_reconciled_zero_usage_turn_is_still_trusted() -> None:
    stats = ParseStats(lines=10, events=10, zero_usage_turns=2)
    assert make_meta().finalize(at_ms=1, stats=stats).trusted is True


def test_finalize_does_not_mutate_the_original() -> None:
    meta = make_meta()
    meta.finalize(at_ms=1, stats=ParseStats())
    assert meta.finalized is False


# --------------------------------------------------------------------------
# The file itself
# --------------------------------------------------------------------------


def test_write_is_atomic_and_leaves_no_temporary(tmp_path: Path) -> None:
    """A kill during the finalizing write must not truncate a source of truth."""
    path = tmp_path / "meta.json"
    write_meta_sync(path, make_meta())
    write_meta_sync(path, make_meta().finalize(at_ms=9, stats=ParseStats()))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["meta.json"]
    assert read_meta_sync(path).finalized_ms == 9


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    meta = make_meta(
        argv=("ai-jail", "--mask", ".env", "codex", "exec", "--json"),
        harness_version="0.101.0",
        model="gpt-5.6-terra",
        env={"PATH": "/usr/bin"},
    ).finalize(at_ms=2, stats=ParseStats(lines=3, events=3))
    path = tmp_path / "meta.json"
    write_meta_sync(path, meta)
    assert read_meta_sync(path) == meta


def test_meta_lives_beside_the_event_log() -> None:
    assert meta_path(Path("/runs"), RUN_ID) == Path("/runs") / RUN_ID / "meta.json"


def test_a_missing_file_is_an_error_not_an_empty_meta(tmp_path: Path) -> None:
    with pytest.raises(MetaError, match="cannot read run metadata"):
        read_meta_sync(tmp_path / "nope.json")


def test_a_damaged_file_refuses_to_load(tmp_path: Path) -> None:
    path = tmp_path / "meta.json"
    path.write_text('{"run_id": "run_1"', encoding="utf-8")
    with pytest.raises(MetaError, match="not valid run metadata"):
        read_meta_sync(path)


def test_an_unexpected_key_refuses_to_load(tmp_path: Path) -> None:
    """``extra="forbid"``: a renamed field must not be silently dropped."""
    path = tmp_path / "meta.json"
    payload = make_meta().model_dump(mode="json")
    payload["price_tabel_version"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(MetaError):
        read_meta_sync(path)


def test_a_file_written_before_a_field_existed_still_loads(tmp_path: Path) -> None:
    """New fields are optional with a default; old runs keep replaying."""
    path = tmp_path / "meta.json"
    payload = make_meta().model_dump(mode="json")
    for optional in ("finalized_ms", "parser", "harness_version", "model", "argv"):
        payload.pop(optional)
    path.write_text(json.dumps(payload), encoding="utf-8")

    recovered = read_meta_sync(path)
    assert recovered.argv == ()
    assert recovered.trusted is False


def test_attempt_must_be_a_real_attempt_number() -> None:
    with pytest.raises(ValueError, match="attempt"):
        make_meta(attempt=0)
