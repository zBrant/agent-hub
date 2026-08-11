"""REST transport for persisted AI defaults."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.api.deps import ai_settings
from app.config import PlannerBackendName, PlannerEffort
from app.preferences import (
    AiOption,
    AiRuntimeSelection,
    AiSettingsSnapshot,
    AiSettingsUpdate,
    AiSettingsValidationError,
)

router = APIRouter(prefix="/api/settings/ai", tags=["settings"])


class AiRuntimeSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: PlannerBackendName
    harness: str | None
    model: str | None

    def to_domain(self) -> AiRuntimeSelection:
        return AiRuntimeSelection(
            backend=self.backend,
            harness=self.harness,
            model=self.model,
        )


class AiSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planner: AiRuntimeSelectionRequest
    search: AiRuntimeSelectionRequest
    planner_effort: PlannerEffort

    def to_domain(self) -> AiSettingsUpdate:
        return AiSettingsUpdate(
            planner=self.planner.to_domain(),
            search=self.search.to_domain(),
            planner_effort=self.planner_effort,
        )


class AiRuntimeSelectionResponse(BaseModel):
    backend: PlannerBackendName
    harness: str | None
    model: str | None

    @classmethod
    def from_domain(cls, value: AiRuntimeSelection) -> AiRuntimeSelectionResponse:
        return cls(
            backend=value.backend,
            harness=value.harness,
            model=value.model,
        )


class AiOptionResponse(BaseModel):
    backend: PlannerBackendName
    harness: str | None
    models: tuple[str, ...]
    is_spend: bool
    supports_effort: bool

    @classmethod
    def from_domain(cls, value: AiOption) -> AiOptionResponse:
        return cls(
            backend=value.backend,
            harness=value.harness,
            models=value.models,
            is_spend=value.is_spend,
            supports_effort=value.supports_effort,
        )


class AiSettingsResponse(BaseModel):
    planner: AiRuntimeSelectionResponse
    search: AiRuntimeSelectionResponse
    planner_effort: PlannerEffort
    planner_options: tuple[AiOptionResponse, ...]
    search_options: tuple[AiOptionResponse, ...]

    @classmethod
    def from_domain(cls, value: AiSettingsSnapshot) -> AiSettingsResponse:
        return cls(
            planner=AiRuntimeSelectionResponse.from_domain(value.planner),
            search=AiRuntimeSelectionResponse.from_domain(value.search),
            planner_effort=value.planner_effort,
            planner_options=tuple(
                AiOptionResponse.from_domain(option) for option in value.planner_options
            ),
            search_options=tuple(
                AiOptionResponse.from_domain(option) for option in value.search_options
            ),
        )


@router.get("", response_model=AiSettingsResponse)
async def get_ai_settings(request: Request) -> AiSettingsResponse:
    return AiSettingsResponse.from_domain(await ai_settings(request).get())


@router.put("", response_model=AiSettingsResponse)
async def put_ai_settings(
    body: AiSettingsRequest, request: Request
) -> AiSettingsResponse:
    try:
        result = await ai_settings(request).update(body.to_domain())
    except AiSettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AiSettingsResponse.from_domain(result)


__all__ = [
    "AiOptionResponse",
    "AiRuntimeSelectionRequest",
    "AiRuntimeSelectionResponse",
    "AiSettingsRequest",
    "AiSettingsResponse",
    "router",
]
