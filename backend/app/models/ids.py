"""Prefixed ULID identifiers (docs/conventions.md §2).

ULIDs sort by creation time, which makes ``ORDER BY id`` meaningful and keeps
logs readable. The prefix says what the id refers to without a lookup.
"""

from ulid import ULID

type SessionId = str
type NodeId = str
type RunId = str


def new_session_id() -> SessionId:
    return f"sess_{ULID()}"


def new_node_id() -> NodeId:
    return f"node_{ULID()}"


def new_run_id() -> RunId:
    return f"run_{ULID()}"
