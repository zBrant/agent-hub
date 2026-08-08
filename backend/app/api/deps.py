"""Shared transport plumbing for the REST routers.

`docs/architecture.md` §1 rule 3: a route validates its input, calls one
``orchestrator/`` use case, and serializes the result. Everything in this module
is one of those three things. There is deliberately no state machine here — the
status codes below are a *translation* of the orchestrator's own error
vocabulary, not a second opinion about what is legal.

**Addressing is validation, not a business rule.** :func:`resolve_node` answers
"does this URL name a real node of this session?", which is the same question
FastAPI's path parsing answers one level up, and its only outcome is 404. It
does not consult the node's status, and no route below it may.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol, cast

from fastapi import HTTPException, Request

from app.models.tables import Node
from app.orchestrator.service import (
    InvalidGraphError,
    InvalidTransitionError,
    NodeRunService,
    ResourceNotFoundError,
)


class NodeStatusPublisher(Protocol):
    """The one broker call the REST layer makes.

    A structural type rather than an import of :class:`app.ws.broker.EventBroker`
    so that ``api/`` and ``ws/`` stay siblings that do not depend on each other
    (`docs/architecture.md` §1 puts them in one layer, not in a stack).
    """

    async def publish_node_status(
        self, *, session_id: str, node_id: str, status: str, ts: int
    ) -> None: ...


def service(request: Request) -> NodeRunService:
    return cast(NodeRunService, request.app.state.orchestrator)


def publisher(request: Request) -> NodeStatusPublisher:
    return cast(NodeStatusPublisher, request.app.state.broker)


async def call[T](operation: Awaitable[T]) -> T:
    """Run one orchestrator use case and translate its refusals.

    The four cases are the orchestrator's whole public error vocabulary:

    ``ResourceNotFoundError`` → **404**
        the address names nothing.
    ``InvalidGraphError`` → **422**
        the *body* describes a graph that is not a DAG. Unprocessable content
        rather than a conflict: nothing about persisted state is wrong, the
        proposal is. The typed defects travel in the detail, because
        `design.md` §8's correction loop needs the node ids and not prose.
    ``InvalidTransitionError`` → **409**
        persisted state does not permit the operation. This is the state check,
        and it lives entirely in ``orchestrator/``.
    ``ValueError`` → **422**
        an argument the orchestrator rejected — an unknown harness, an
        unsupported model, blank feedback.
    """
    try:
        return await operation
    except ResourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidGraphError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "errors": [
                    {
                        "kind": error.kind.value,
                        "nodes": list(error.nodes),
                        "message": error.message,
                    }
                    for error in exc.errors
                ],
            },
        ) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def resolve_node(request: Request, session_id: str, node_id: str) -> Node:
    """The node this URL addresses, or 404.

    Both halves matter: a session that does not exist is a 404 raised by
    ``list_nodes``, and a node id that is not in *this* session is a 404 raised
    here. Guessing — treating ``/sessions/A/nodes/<node of B>`` as valid because
    the id resolves globally — would let one session's URL operate on another's
    worktree.
    """
    nodes = await call(service(request).list_nodes(session_id))
    for node in nodes:
        if node.id == node_id:
            return node
    raise HTTPException(
        status_code=404, detail=f"no such node {node_id} in session {session_id}"
    )


async def announce(request: Request, node: Node) -> None:
    """Broadcast one node transition, after the orchestrator persisted it.

    Called with a row that was **read back** from the database, never with a
    status the route inferred. `docs/architecture.md` §4 fixes the order as
    NDJSON → SQLite → broadcast, and a transport that published its own guess
    would put a frame on the wire describing state that does not exist yet.
    """
    await publisher(request).publish_node_status(
        session_id=node.session_id,
        node_id=node.id,
        status=node.status.value,
        ts=node.updated_ms,
    )


__all__ = [
    "NodeStatusPublisher",
    "announce",
    "call",
    "publisher",
    "resolve_node",
    "service",
]
