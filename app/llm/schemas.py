"""LLM structured output 스키마. M6.2에서 patch generation에 사용."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["replace", "add", "remove"]
    path: str = Field(description="dot-separated path within the YAML, e.g. collectors.jobs.mapping.items_path")
    old: str | None = Field(default=None, description="변경 전 값 (replace/remove에 명시)")
    new: str | None = Field(default=None, description="변경 후 값 (replace/add에 명시)")


class PatchCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str = Field(description="대상 YAML 파일 경로 (예: configs/sites/catch.yaml)")
    changes: list[PatchOperation]
    reason: str = Field(description="변경 근거 (한국어, 간결히)")
    risk: Literal["low", "medium", "high"]


class CollectionInfeasible(BaseModel):
    """Red Zone 등으로 patch 생성이 불가능할 때."""

    model_config = ConfigDict(extra="forbid")

    site: str
    category: Literal["red_zone", "out_of_scope", "needs_human"]
    reason: str
