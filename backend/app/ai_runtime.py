"""Composition of persisted AI choices with provider and harness backends.

This module sits above both the search vertical and harness adapters. Keeping
the bridge here preserves the rule that ``app.search`` never imports or
branches on a harness while still letting both CLI subscriptions implement the
same bounded tool loop.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from app.config import Settings
from app.harnesses import create_adapter
from app.harnesses.base import (
    HarnessError,
    StructuredCompleter,
    StructuredRequest,
    supports_structured_output,
)
from app.models.pricing import PriceTable
from app.preferences import AiRuntimeSelection
from app.sandbox.aijail import build_launcher, default_policy
from app.search.agent import (
    HarnessSearchRequest,
    SearchAgent,
    create_harness_search_client,
    create_search_agent,
)
from app.search.tools import CodeSearchService


def _structured_request(request: HarnessSearchRequest) -> StructuredRequest:
    return StructuredRequest(
        prompt=request.prompt,
        schema=request.schema,
        system=request.system,
        model=request.model,
        cwd=request.cwd,
        env=request.env,
        launcher=request.launcher,
    )


def create_runtime_search_agent(
    *,
    selection: AiRuntimeSelection,
    tools: CodeSearchService,
    settings: Settings,
    prices: PriceTable,
) -> SearchAgent:
    """Build one request-owned Search agent from a validated preference."""
    if selection.backend == "api":
        if selection.harness is not None or selection.model is None:
            raise ValueError("invalid persisted API Code Search runtime")
        effective = settings.model_copy(update={"search_model": selection.model})
        return create_search_agent(tools=tools, settings=effective, prices=prices)

    if selection.harness is None:
        raise ValueError("persisted harness Code Search runtime has no harness")
    adapter = create_adapter(selection.harness)
    if not supports_structured_output(adapter):
        raise ValueError(
            f"harness {selection.harness!r} cannot return structured search actions"
        )
    effective = settings.model_copy(
        update={"search_model": selection.model or f"{adapter.name}:default"}
    )
    policy = replace(default_policy(), worktree=False)
    client = create_harness_search_client(
        completer=cast(StructuredCompleter, adapter),
        request_factory=_structured_request,
        model=selection.model,
        launcher=tuple(build_launcher(policy)),
        max_transcript_bytes=settings.search_max_bytes,
        backend_errors=(HarnessError, OSError),
    )
    return SearchAgent(
        client=client,
        tools=tools,
        settings=effective,
        prices=prices,
    )


__all__ = ["create_runtime_search_agent"]
