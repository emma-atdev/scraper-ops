"""Site YAML config의 pydantic schema.

LLM이 HITL 경유로 생성·수정할 수 있는 유일한 영역. 스키마는 보수적으로 유지하고
필드 추가 시 명시적으로 한다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RequestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST", "PUT", "DELETE"] = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] | None = None


class PaginationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["none", "page", "cursor"] = "none"
    param: str | None = None
    start: int = 1
    max_pages: int = 50
    stop_condition: Literal["empty_items", "fixed_pages"] = "empty_items"


class MappingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items_path: str
    fields: dict[str, str] = Field(default_factory=dict)
    link_template: str | None = None


class ValidationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_fields: list[str] = Field(default_factory=list)
    min_items_per_page: int = 0
    max_volume_drop_ratio: float = 0.8


class StaticDiagnosticsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    save_snapshot: bool = True
    blocked_signals: list[str] = Field(default_factory=list)


class CollectorConfig(BaseModel):
    """단일 collector 정의. type별로 필요한 sub-config는 optional로 둔다."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["api_jobs", "static_html", "detail_html"]
    fetcher: Literal["http", "dynamic", "stealthy"] = "http"
    purpose: Literal["postings", "diagnostics", "enrichment"] = "postings"
    requires_approval: bool = False
    request: RequestConfig | None = None
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
    mapping: MappingConfig | None = None
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    diagnostics: StaticDiagnosticsConfig | None = None


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_environment: Literal["local", "vm", "ci", "container"] = "vm"
    min_interval_minutes: int = 60
    request_delay_seconds: float = 2.0
    max_pages_per_run: int = 100


class SiteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: str
    name: str
    enabled: bool = True
    collectors: dict[str, CollectorConfig]
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
