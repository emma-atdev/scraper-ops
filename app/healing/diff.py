"""PatchCandidate를 사람이 읽기 좋은 unified-diff 비슷한 텍스트로 변환.

Slack 메시지·CLI 출력에서 재사용한다. 여기서는 표시만 한다 — 진짜 yaml 적용은 M6.6.
"""

from __future__ import annotations

from app.llm.schemas import PatchCandidate, PatchOperation


def format_patch_diff(patch: PatchCandidate) -> str:
    """PatchCandidate.changes를 줄 단위 diff 문자열로 변환.

    예시 출력:
        collectors.jobs.mapping.items_path:
        - recruitData
        + data.recruitData

        collectors.jobs.request.headers.Referer:
        + https://www.catch.co.kr/NCS/RecruitSearch

    빈 changes는 "(변경 없음)" 안내 문자열을 돌려준다.
    """
    if not patch.changes:
        return "(변경 없음 — LLM이 처방을 만들지 못했다고 판정)"
    return "\n\n".join(_format_op(op) for op in patch.changes)


def _format_op(op: PatchOperation) -> str:
    head = f"{op.path}:"
    if op.op == "replace":
        return f"{head}\n- {_render(op.old)}\n+ {_render(op.new)}"
    if op.op == "add":
        return f"{head}\n+ {_render(op.new)}"
    if op.op == "remove":
        return f"{head}\n- {_render(op.old)}"
    return f"{head}\n? unknown op: {op.op}"  # pragma: no cover (Literal 강제)


def _render(value: str | None) -> str:
    if value is None:
        return "(none)"
    if value == "":
        return '""'
    return value
