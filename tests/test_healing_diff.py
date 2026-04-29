"""patch diff 포매팅 단위 테스트."""

from __future__ import annotations

from app.healing import format_patch_diff
from app.llm import PatchCandidate, PatchOperation


def _patch(changes: list[dict]) -> PatchCandidate:
    return PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(**c) for c in changes],
        reason="test",
        risk="low",
    )


def test_format_replace():
    out = format_patch_diff(_patch([
        {"op": "replace", "path": "collectors.jobs.mapping.items_path",
         "old": "recruitData", "new": "data.recruitData"},
    ]))
    assert "collectors.jobs.mapping.items_path:" in out
    assert "- recruitData" in out
    assert "+ data.recruitData" in out


def test_format_add():
    out = format_patch_diff(_patch([
        {"op": "add", "path": "collectors.jobs.request.headers.Referer",
         "new": "https://www.catch.co.kr/NCS/RecruitSearch"},
    ]))
    assert "+ https://www.catch.co.kr/NCS/RecruitSearch" in out
    assert "- " not in out


def test_format_remove():
    out = format_patch_diff(_patch([
        {"op": "remove", "path": "collectors.jobs.request.params.ExceptIDList",
         "old": ""},
    ]))
    assert "- " in out
    assert '""' in out  # 빈 문자열 표기


def test_format_multiple_changes_separated_by_blank_line():
    out = format_patch_diff(_patch([
        {"op": "replace", "path": "a.b", "old": "x", "new": "y"},
        {"op": "add", "path": "c.d", "new": "z"},
    ]))
    blocks = out.split("\n\n")
    assert len(blocks) == 2


def test_format_empty_changes():
    out = format_patch_diff(_patch([]))
    assert "변경 없음" in out


def test_format_none_value_rendered_explicitly():
    out = format_patch_diff(_patch([
        {"op": "replace", "path": "a.b", "old": None, "new": "x"},
    ]))
    assert "(none)" in out
