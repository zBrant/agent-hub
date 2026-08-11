"""Global AI defaults, independent from the runtimes that consume them.

This module owns validation and persistence only. Planner and search keep using
their existing settings until their integration explicitly asks this service
for a snapshot. That separation makes a saved preference durable without
silently changing an in-flight request.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

from app.config import PlannerBackendName, PlannerEffort, Settings
from app.harnesses import ADAPTERS, create_adapter
from app.harnesses.base import supports_structured_output
from app.models.tables import AiPreference
from app.storage.db import Database
from app.storage.repository import Repository


class AiSettingsValidationError(ValueError):
    """A saved backend, harness, model, or effort cannot be used."""


@dataclass(frozen=True, slots=True)
class AiRuntimeSelection:
    """A fully resolved backend selection for one AI-backed feature."""

    backend: PlannerBackendName
    harness: str | None
    model: str | None


@dataclass(frozen=True, slots=True)
class AiOption:
    """One capability-derived backend choice exposed to settings clients."""

    backend: PlannerBackendName
    harness: str | None
    models: tuple[str, ...]
    is_spend: bool
    supports_effort: bool


@dataclass(frozen=True, slots=True)
class AiSettingsUpdate:
    """The complete authored value accepted by :meth:`AiSettingsService.update`."""

    planner: AiRuntimeSelection
    search: AiRuntimeSelection
    planner_effort: PlannerEffort


@dataclass(frozen=True, slots=True)
class AiSettingsSnapshot:
    """Effective selections plus the catalogs needed to edit them."""

    planner: AiRuntimeSelection
    search: AiRuntimeSelection
    planner_effort: PlannerEffort
    planner_options: tuple[AiOption, ...]
    search_options: tuple[AiOption, ...]
    is_persisted: bool = False


def selectable_ai_harnesses() -> dict[str, tuple[str, ...]]:
    """Installed adapters that implement the structured-completion capability."""
    catalog: dict[str, tuple[str, ...]] = {}
    for name in sorted(ADAPTERS):
        adapter = create_adapter(name)
        if supports_structured_output(adapter):
            catalog[name] = tuple(adapter.supported_models)
    return catalog


def _options(
    api_models: list[str],
    harnesses: dict[str, tuple[str, ...]],
    *,
    api_supports_effort: bool,
) -> tuple[AiOption, ...]:
    return (
        AiOption(
            backend="api",
            harness=None,
            models=tuple(api_models),
            is_spend=True,
            supports_effort=api_supports_effort,
        ),
        *(
            AiOption(
                backend="harness",
                harness=name,
                models=models,
                is_spend=False,
                supports_effort=False,
            )
            for name, models in harnesses.items()
        ),
    )


def _validate_selection(
    feature: str,
    selection: AiRuntimeSelection,
    *,
    api_models: list[str],
    harnesses: dict[str, tuple[str, ...]],
) -> None:
    if selection.backend == "api":
        if selection.harness is not None:
            raise AiSettingsValidationError(
                f"{feature} API backend runs without a harness"
            )
        if selection.model is None or selection.model not in api_models:
            raise AiSettingsValidationError(
                f"{feature} API model must be one of {api_models}"
            )
        return

    if selection.backend != "harness":
        raise AiSettingsValidationError(
            f"unknown {feature} backend {selection.backend!r}; "
            "valid: ['api', 'harness']"
        )
    harness = selection.harness
    if harness is None or harness not in harnesses:
        raise AiSettingsValidationError(
            f"{feature} harness must support structured completion; "
            f"valid: {sorted(harnesses)}"
        )
    if selection.model is not None and selection.model not in harnesses[harness]:
        raise AiSettingsValidationError(
            f"{feature} model {selection.model!r} is not supported by harness "
            f"{harness!r}; valid: {list(harnesses[harness])}"
        )


class AiSettingsService:
    """Read, validate, and persist the singleton AI settings document."""

    def __init__(self, *, database: Database, settings: Settings) -> None:
        self._database = database
        self._settings = settings
        self._write_lock = asyncio.Lock()

    def _defaults(self) -> AiSettingsUpdate:
        settings = self._settings
        planner = (
            AiRuntimeSelection("api", None, settings.planner_model)
            if settings.planner_backend == "api"
            else AiRuntimeSelection(
                "harness", settings.planner_harness, settings.planner_harness_model
            )
        )
        search = (
            AiRuntimeSelection("api", None, settings.search_model)
            if settings.search_backend == "api"
            else AiRuntimeSelection(
                "harness", settings.search_harness, settings.search_harness_model
            )
        )
        return AiSettingsUpdate(
            planner=planner,
            search=search,
            planner_effort=settings.planner_effort,
        )

    @staticmethod
    def _from_row(row: AiPreference) -> AiSettingsUpdate:
        return AiSettingsUpdate(
            planner=AiRuntimeSelection(
                backend=cast(PlannerBackendName, row.planner_backend),
                harness=row.planner_harness,
                model=row.planner_model,
            ),
            search=AiRuntimeSelection(
                backend=cast(PlannerBackendName, row.search_backend),
                harness=row.search_harness,
                model=row.search_model,
            ),
            planner_effort=cast(PlannerEffort, row.planner_effort),
        )

    def _snapshot(
        self, value: AiSettingsUpdate, *, is_persisted: bool
    ) -> AiSettingsSnapshot:
        harnesses = selectable_ai_harnesses()
        return AiSettingsSnapshot(
            planner=value.planner,
            search=value.search,
            planner_effort=value.planner_effort,
            planner_options=_options(
                self._settings.planner_api_models,
                harnesses,
                api_supports_effort=True,
            ),
            search_options=_options(
                self._settings.search_api_models,
                harnesses,
                api_supports_effort=False,
            ),
            is_persisted=is_persisted,
        )

    async def get(self) -> AiSettingsSnapshot:
        """Return persisted values, falling back to deployment settings."""
        async with self._database.session() as session:
            row = await Repository(session).get_ai_preference()
        return self._snapshot(
            self._defaults() if row is None else self._from_row(row),
            is_persisted=row is not None,
        )

    async def update(self, value: AiSettingsUpdate) -> AiSettingsSnapshot:
        """Validate and atomically replace the complete singleton document."""
        harnesses = selectable_ai_harnesses()
        _validate_selection(
            "planner",
            value.planner,
            api_models=self._settings.planner_api_models,
            harnesses=harnesses,
        )
        _validate_selection(
            "search",
            value.search,
            api_models=self._settings.search_api_models,
            harnesses=harnesses,
        )
        async with self._write_lock:
            async with self._database.session() as session:
                await Repository(session).upsert_ai_preference(
                    planner_backend=value.planner.backend,
                    planner_harness=value.planner.harness,
                    planner_model=value.planner.model,
                    search_backend=value.search.backend,
                    search_harness=value.search.harness,
                    search_model=value.search.model,
                    planner_effort=value.planner_effort,
                )
        return self._snapshot(value, is_persisted=True)


__all__ = [
    "AiOption",
    "AiRuntimeSelection",
    "AiSettingsService",
    "AiSettingsSnapshot",
    "AiSettingsUpdate",
    "AiSettingsValidationError",
    "selectable_ai_harnesses",
]
