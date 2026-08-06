"""The Phase 0 driver wired through real git worktrees and event storage."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.harnesses.base import ParseStats, RunSpec
from app.harnesses.events import AgentEvent, RunFinished, TurnFinished, Usage
from app.models.pricing import load_price_table
from app.storage.ndjson import read_events
from scripts import spike

SESSION_ID = "sess_SPIKETEST"
RUN_ID = "run_SPIKETEST"


async def git(cwd: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    output = stdout.decode()
    assert process.returncode == 0, f"git {args} failed:\n{output}"
    return output


@pytest.fixture
async def repo(tmp_path: Path) -> Path:
    path = tmp_path / "target"
    path.mkdir()
    await git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("fixture\n")
    await git(path, "add", "-A")
    await git(path, "commit", "-qm", "initial")
    return path


class FakeAdapter:
    """Writes one file, then supplies a minimal successful event stream."""

    name = "fake"

    def __init__(self, *, parser_drift: bool = False) -> None:
        self.supported_models = ["claude-haiku-4-5"]
        unknown = {"future_line": 1} if parser_drift else {}
        self.stats = ParseStats(lines=2, events=2, unknown=unknown)
        self.spec: RunSpec | None = None

    async def start(self, spec: RunSpec) -> object:
        self.spec = spec
        await asyncio.to_thread(
            (spec.cwd / "agent.txt").write_text,
            "written by the fake harness\n",
            encoding="utf-8",
        )
        return object()

    def build_argv(self, _spec: RunSpec) -> list[str]:
        return ["fake", "exec", "-"]

    async def events(self, _handle: object) -> AsyncIterator[AgentEvent]:
        assert self.spec is not None
        yield Usage(
            run_id=self.spec.run_id,
            ts=1,
            model="claude-haiku-4-5",
            input_tokens=3,
            output_tokens=5,
        )
        yield TurnFinished(
            run_id=self.spec.run_id,
            ts=2,
            turn=1,
            status="success",
        )
        yield RunFinished(
            run_id=self.spec.run_id,
            ts=3,
            status="success",
            exit_code=0,
        )

    async def kill(self, _handle: object) -> None:
        return None


@pytest.mark.parametrize("parser_drift", [False, True])
async def test_driver_merges_only_a_fully_trusted_run(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parser_drift: bool,
) -> None:
    monkeypatch.setattr(spike, "new_session_id", lambda: SESSION_ID)
    monkeypatch.setattr(spike, "new_run_id", lambda: RUN_ID)
    monkeypatch.setattr(
        spike,
        "create_adapter",
        lambda _name: FakeAdapter(parser_drift=parser_drift),
    )

    workspaces = tmp_path / "workspaces"
    runs = tmp_path / "runs"
    result = await spike.run_spike(
        repo=repo,
        prompt="write agent.txt",
        harness="codex",
        model="claude-haiku-4-5",
        workspaces_root=workspaces,
        runs_root=runs,
        prices=load_price_table(spike.DEFAULT_PRICING),
        budget_usd=None,
    )

    integration_file = workspaces / SESSION_ID / "integration" / "agent.txt"
    node_file = workspaces / SESSION_ID / spike.NODE_ID / "agent.txt"
    assert node_file.read_text() == "written by the fake harness\n"
    assert integration_file.exists() is (not parser_drift)
    assert result == int(parser_drift)

    replayed = list(read_events(runs / RUN_ID / "events.ndjson"))
    assert [event.type for event in replayed] == [
        "usage",
        "turn_finished",
        "run_finished",
    ]


def test_report_rejects_missing_or_unreconciled_usage() -> None:
    prices = load_price_table(spike.DEFAULT_PRICING)
    finished: list[AgentEvent] = [
        RunFinished(run_id=RUN_ID, ts=1, status="success", exit_code=0)
    ]
    assert not spike.report(finished, ParseStats(), prices)

    events = [
        Usage(run_id=RUN_ID, ts=1, model="claude-haiku-4-5", input_tokens=1),
        *finished,
    ]
    stats = ParseStats(zero_usage_turns=1, usage_unreconciled_turns=1)
    assert not spike.report(events, stats, prices)
