"""L4 建议通道 + 覆盖可观测性。

设计依据：
- discuss/02 §2.1：架构/体验类纯 L4 发现走"建议通道"，**绝不混进 bug 报告**；
  独立产出、低频聚合、明确标注非 bug。
- discuss/06 §3：选靶以 semantic-map 未覆盖路径为主轴；可观测性需回答
  "哪些路径没测 / 为何没挖到"。

铁律：
- 建议通道与 bug queue **物理隔离**：本模块只写独立的 suggestion sink，
  从不调用 publishing.create_bug_report。
- L4 永不立案：建议是"线索/选靶/架构观察"，需要人转化为 L1-L3 case 才进闸门。

本模块产品中立、离线可单测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


@dataclass(frozen=True)
class Suggestion:
    """一条建议（非 bug）。"""

    kind: str                      # "architecture" | "target" | "coverage_gap" | "clue"
    summary: str
    semantic_map_entry_id: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "semantic_map_entry_id": self.semantic_map_entry_id,
            "detail": self.detail,
            "is_bug": False,          # 永远标注非 bug，防混流
        }


class SuggestionSink:
    """独立建议产出。与 bug queue 物理隔离：只写自己的 JSONL。"""

    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else None
        self._buffer: List[Suggestion] = []

    def add(self, suggestion: Suggestion) -> None:
        self._buffer.append(suggestion)

    def extend(self, suggestions: Sequence[Suggestion]) -> None:
        self._buffer.extend(suggestions)

    def items(self) -> List[Suggestion]:
        return list(self._buffer)

    def flush(self) -> Optional[Path]:
        """低频聚合写盘。返回写入路径；无 path 则只留内存。"""

        if self.path is None:
            return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for item in self._buffer:
                handle.write(json.dumps({"kind": "suggestion", "payload": item.as_dict()},
                                        ensure_ascii=False, sort_keys=True) + "\n")
        flushed = self.path
        self._buffer.clear()
        return flushed


@dataclass(frozen=True)
class CoverageReport:
    """覆盖追踪结果：semantic-map 里哪些 entry 被本批 case 覆盖。"""

    total_entries: int
    covered_entry_ids: List[str]
    uncovered_entry_ids: List[str]

    @property
    def covered_count(self) -> int:
        return len(self.covered_entry_ids)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "total_entries": self.total_entries,
            "covered_count": self.covered_count,
            "uncovered_count": len(self.uncovered_entry_ids),
            "uncovered_entry_ids": list(self.uncovered_entry_ids),
        }


def compute_coverage(
    semantic_map: Mapping[str, Any],
    exercised_entry_ids: Sequence[str],
) -> CoverageReport:
    """对照 semantic-map 全量 entry 与本批实际触达的 entry，算未覆盖路径。"""

    all_ids: List[str] = []
    entries = semantic_map.get("entries") if isinstance(semantic_map, Mapping) else None
    for entry in entries or []:
        if isinstance(entry, Mapping):
            eid = str(entry.get("id") or "").strip()
            if eid:
                all_ids.append(eid)
    exercised = {str(e).strip() for e in exercised_entry_ids if str(e).strip()}
    covered = [eid for eid in all_ids if eid in exercised]
    uncovered = [eid for eid in all_ids if eid not in exercised]
    return CoverageReport(total_entries=len(all_ids), covered_entry_ids=covered, uncovered_entry_ids=uncovered)


def coverage_gaps_to_suggestions(report: CoverageReport) -> List[Suggestion]:
    """把未覆盖路径转成"选靶建议"（coverage_gap），喂给后续 L2/L3 选靶。"""

    return [
        Suggestion(
            kind="coverage_gap",
            summary=f"semantic-map entry 未被任何 case 覆盖：{eid}",
            semantic_map_entry_id=eid,
            detail={"action": "为该 entry 补 L2 spec 或 L3 关系"},
        )
        for eid in report.uncovered_entry_ids
    ]
