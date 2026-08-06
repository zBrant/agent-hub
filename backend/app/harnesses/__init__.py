"""Harness adapter registry; the only name-to-implementation dispatch point."""

from app.harnesses.base import BaseHarnessAdapter
from app.harnesses.claude_code import ClaudeCodeAdapter
from app.harnesses.codex import CodexAdapter


class UnknownHarnessError(ValueError):
    """A requested harness has no installed adapter."""


ADAPTERS: dict[str, type[ClaudeCodeAdapter] | type[CodexAdapter]] = {
    "claude-code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}


def create_adapter(name: str) -> BaseHarnessAdapter:
    try:
        factory = ADAPTERS[name]
    except KeyError as exc:
        raise UnknownHarnessError(
            f"unknown harness {name!r}; available: {sorted(ADAPTERS)}"
        ) from exc
    return factory()


__all__ = ["ADAPTERS", "UnknownHarnessError", "create_adapter"]
